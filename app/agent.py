from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import boto3
from playwright.async_api import BrowserContext, Page

from .security import validate_public_url

MODEL_ID = os.getenv("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-6")
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")

TOOLS: list[dict[str, Any]] = [
    {
        "name": "click",
        "description": "Click a visible element. Prefer target with index, id, role/name, label, placeholder, text, or testId; selector is supported for legacy use. Use click_count 1 for a normal click, 2 for a double-click, or 3 for a triple-click.",
        "parameters": {
            "type": "object",
            "properties": {
                "selector": {"type": "string"},
                "target": {"type": "object", "description": "Preferred structured locator: {index}, {id}, {role,name}, {label}, {placeholder}, {text}, {testId}, or {name}."},
                "click_count": {"type": "integer", "minimum": 1, "maximum": 3, "default": 1},
            },
        },
    },
    {
        "name": "click_at",
        "description": "Click a viewport coordinate. Use click_count 1 for a normal click, 2 for a double-click, or 3 for a triple-click.",
        "parameters": {
            "type": "object",
            "properties": {
                "x": {"type": "number"},
                "y": {"type": "number"},
                "click_count": {"type": "integer", "minimum": 1, "maximum": 3, "default": 1},
            },
            "required": ["x", "y"],
        },
    },
    {
        "name": "type_text",
        "description": "Fill a visible input, textarea, or contenteditable element. Prefer target with label, placeholder, name, id, role, or index; selector is supported for legacy use.",
        "parameters": {"type": "object", "properties": {"selector": {"type": "string"}, "target": {"type": "object"}, "text": {"type": "string"}}, "required": ["text"]},
    },
    {
        "name": "press_key",
        "description": "Press a keyboard key such as Enter, Escape, ArrowLeft, or Space.",
        "parameters": {"type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"]},
    },
    {
        "name": "select_option",
        "description": "Select an option in a native HTML select element using a structured target or legacy selector.",
        "parameters": {"type": "object", "properties": {"selector": {"type": "string"}, "target": {"type": "object"}, "value": {"type": "string"}}, "required": ["value"]},
    },
    {
        "name": "drag",
        "description": "Drag from one viewport coordinate to another. Useful for canvas games and drawing applications.",
        "parameters": {"type": "object", "properties": {"start_x": {"type": "number"}, "start_y": {"type": "number"}, "end_x": {"type": "number"}, "end_y": {"type": "number"}}, "required": ["start_x", "start_y", "end_x", "end_y"]},
    },
    {
        "name": "scroll",
        "description": "Scroll the page vertically.",
        "parameters": {"type": "object", "properties": {"amount": {"type": "integer", "minimum": -2000, "maximum": 2000}}, "required": ["amount"]},
    },
    {
        "name": "wait",
        "description": "Wait for a JavaScript-heavy page or multi-step onboarding screen to render.",
        "parameters": {"type": "object", "properties": {"milliseconds": {"type": "integer", "minimum": 250, "maximum": 10000, "default": 1000}}, "required": []},
    },
    {
        "name": "navigate",
        "description": "Navigate back or forward in browser history, or reload the current page.",
        "parameters": {"type": "object", "properties": {"direction": {"type": "string", "enum": ["back", "forward", "reload"]}}, "required": ["direction"]},
    },
    {
        "name": "focus",
        "description": "Focus a visible input or other target without changing its value.",
        "parameters": {"type": "object", "properties": {"selector": {"type": "string"}, "target": {"type": "object"}}, "required": []},
    },
    {
        "name": "screenshot",
        "description": "Save a screenshot of the current page state.",
        "parameters": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
    },
    {
        "name": "submit_form",
        "description": "Submit the primary form. If the task requires confirmation, this pauses before submission; otherwise it submits the form.",
        "parameters": {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]},
    },
    {
        "name": "finish",
        "description": "Finish when the user request is complete or cannot be completed.",
        "parameters": {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]},
    },
]

BEDROCK_TOOLS = [
    {
        "toolSpec": {
            "name": item["name"],
            "description": item["description"],
            "inputSchema": {"json": item["parameters"]},
        }
    }
    for item in TOOLS
]

SYSTEM_PROMPT = """You are a browser agent. Complete the user's instruction by observing the current page and using only the provided browser tools.

Rules:
- Never invent that an action succeeded; inspect the page after actions.
- Prefer visible, public UI controls and structured targets based on the latest observation. Do not bypass CAPTCHAs, authentication, paywalls, or access controls.
- Before clicking, use the current control index, accessible role/name, label, placeholder, text, or stable id. Do not reuse an index after navigation without observing again.
- If an action fails, try a different locator strategy, scroll the target into view, wait for rendering, or inspect the updated page before retrying. Do not repeat the identical failed action indefinitely.
- Treat a click as successful only after observing a state change, URL change, dialog, validation message, or other evidence.
- Use wait for JavaScript-heavy pages, navigate for history/reload recovery, and screenshot when the visual layout is needed.
- For games and canvas apps, use drag, click, and press_key based on observed canvas dimensions and visible UI.
- Do not submit forms, make purchases, send messages, or change account/security settings without the submit_form tool. The API may require confirmation before that tool submits.
- Keep the task focused and stop with finish when complete or blocked.
"""


@dataclass
class AgentSession:
    task_id: str
    context: BrowserContext
    page: Page
    messages: list[dict[str, Any]]
    max_steps: int
    timeout_seconds: int
    artifact_dir: Path
    require_confirmation: bool = False
    step_count: int = 0
    pending_confirmation: dict[str, Any] | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    last_observation_hash: str | None = None
    action_failures: int = 0


class AgentRunner:
    def __init__(self, browser, artifact_dir: Path) -> None:
        self.browser = browser
        self.artifact_dir = artifact_dir
        self.client = boto3.client("bedrock-runtime", region_name=AWS_REGION)

    async def start_session(
        self,
        task_id: str,
        url: str,
        instruction: str,
        max_steps: int,
        timeout_seconds: int,
        require_confirmation: bool = False,
    ) -> AgentSession:
        validate_public_url(url)
        # Some test environments use a self-signed or otherwise incomplete TLS
        # certificate. The browser agent must still be able to inspect those
        # explicitly requested sites; URL safety validation remains enforced.
        context = await self.browser.new_context(
            viewport={"width": 1440, "height": 900},
            ignore_https_errors=True,
        )
        page = await context.new_page()
        page.set_default_timeout(timeout_seconds * 1000)
        await page.goto(url, wait_until="domcontentloaded", timeout=timeout_seconds * 1000)
        session = AgentSession(
            task_id=task_id,
            context=context,
            page=page,
            messages=[
                {"role": "user", "content": [{"text": f"User instruction: {instruction}\nInitial page observation:\n{await self.observe(page)}"}]},
            ],
            max_steps=max_steps,
            timeout_seconds=timeout_seconds,
            artifact_dir=self.artifact_dir,
            require_confirmation=require_confirmation,
        )
        return session

    async def run_until_pause(self, session: AgentSession, allow_submission: bool = False) -> dict[str, Any]:
        while session.step_count < session.max_steps:
            session.step_count += 1
            self.compact_messages(session)
            try:
                completion = await asyncio.to_thread(
                    self.client.converse,
                    modelId=MODEL_ID,
                    system=[{"text": SYSTEM_PROMPT}],
                    messages=session.messages,
                    toolConfig={"tools": BEDROCK_TOOLS},
                    inferenceConfig={"maxTokens": 2_000, "temperature": 0.1},
                )
            except Exception as exc:
                session.events.append({"step": session.step_count, "status": "model_error", "error": str(exc)[:500]})
                return {"status": "failed", "error": f"Model call failed: {exc}"}
            assistant_message = completion["output"]["message"]
            session.messages.append(assistant_message)
            content = assistant_message.get("content", [])
            tool_calls = [block["toolUse"] for block in content if "toolUse" in block]
            if not tool_calls:
                summary = "\n".join(block["text"] for block in content if "text" in block) or "The agent stopped without a summary."
                return {"status": "completed", "summary": summary}

            tool_results = []
            for tool_call in tool_calls:
                name = tool_call["name"]
                args = tool_call.get("input") or {}
                event = {"step": session.step_count, "tool": name, "arguments": self._mask_args(name, args)}
                if name == "submit_form" and session.require_confirmation and not allow_submission:
                    session.pending_confirmation = {
                        "reason": args.get("reason", "The agent wants to submit the form."),
                        "url": session.page.url,
                        "tool_call_id": tool_call["toolUseId"],
                    }
                    event["status"] = "waiting_confirmation"
                    session.events.append(event)
                    return {"status": "waiting_confirmation", "confirmation": session.pending_confirmation}

                try:
                    result = await self.execute_tool(session, name, args, allow_submission)
                except Exception as exc:  # Tool failures must become model-visible results.
                    session.action_failures += 1
                    result = {"ok": False, "error": str(exc)[:1_000], "recoverable": True}
                event["result"] = result
                session.events.append(event)
                tool_results.append({
                    "toolResult": {
                        "toolUseId": tool_call["toolUseId"],
                        "content": [{"text": json.dumps(result)}],
                    }
                })

            session.messages.append({"role": "user", "content": tool_results})
            try:
                observation = await self.observe(session.page)
            except Exception as exc:
                observation = json.dumps({"observation_error": str(exc)[:500], "url": session.page.url})
            session.messages.append({
                "role": "user",
                "content": [{"text": f"Updated page observation after step {session.step_count}:\n{observation}"}],
            })

        return {"status": "failed", "error": f"Agent reached the maximum of {session.max_steps} steps"}

    @staticmethod
    def compact_messages(session: AgentSession, max_chars: int = 90_000) -> None:
        """Keep recent interaction context while preventing unbounded growth."""
        encoded_size = len(json.dumps(session.messages, ensure_ascii=False))
        if encoded_size <= max_chars or len(session.messages) <= 5:
            return
        first = session.messages[:1]
        recent = session.messages[-10:]
        session.messages = first + [{
            "role": "user",
            "content": [{"text": "Earlier browser history was compacted. Trust only the current page observation and recent tool results."}],
        }] + recent

    async def resolve_locator(self, page: Page, args: dict[str, Any]):
        target = args.get("target")
        frames = page.frames

        async def first_matching(factory):
            for frame in frames:
                try:
                    candidate = factory(frame)
                    if await candidate.count() > 0:
                        return candidate.first
                except Exception:
                    continue
            raise ValueError(f"No visible element matched target {target!r}")

        if not target:
            selector = args.get("selector")
            if not selector:
                raise ValueError("Provide target or selector")
            return await first_matching(lambda frame: frame.locator(selector))
        if "index" in target:
            visible = "input:visible, textarea:visible, select:visible, button:visible, a:visible, [role='button']:visible, [role='combobox']:visible, [role='textbox']:visible, [role='checkbox']:visible, [role='radio']:visible"
            return page.locator(visible).nth(int(target["index"]))
        if "id" in target:
            return await first_matching(lambda frame: frame.locator(f"#{target['id']}"))
        if "testId" in target:
            return await first_matching(lambda frame: frame.get_by_test_id(target["testId"]))
        if "role" in target:
            return await first_matching(lambda frame: frame.get_by_role(target["role"], name=target.get("name"), exact=target.get("exact", False)))
        if "placeholder" in target:
            return await first_matching(lambda frame: frame.get_by_placeholder(target["placeholder"], exact=target.get("exact", False)))
        if "label" in target:
            return await first_matching(lambda frame: frame.get_by_label(target["label"], exact=target.get("exact", False)))
        if "text" in target:
            text = target["text"]
            exact = target.get("exact", False)
            return await first_matching(lambda frame: frame.get_by_text(text, exact=exact))
        if "name" in target:
            return await first_matching(lambda frame: frame.locator(f"[name='{target['name']}']"))
        if "type" in target:
            return await first_matching(lambda frame: frame.locator(f"input[type='{target['type']}'], button[type='{target['type']}']"))
        raise ValueError(f"Unsupported target shape: {target!r}")

    async def execute_tool(self, session: AgentSession, name: str, args: dict[str, Any], allow_submission: bool) -> dict[str, Any]:
        page = session.page
        timeout = session.timeout_seconds * 1000
        if name == "click":
            click_count = max(1, min(3, int(args.get("click_count", 1))))
            click_timeout = min(timeout, 5_000)
            try:
                locator = await self.resolve_locator(page, args)
                await locator.scroll_into_view_if_needed(timeout=click_timeout)
                await locator.click(click_count=click_count, timeout=click_timeout)
                await asyncio.sleep(0.25)
                if len(session.context.pages) > 1:
                    session.page = session.context.pages[-1]
                with contextlib.suppress(Exception):
                    await session.page.wait_for_load_state("domcontentloaded", timeout=2_000)
                return {"ok": True, "click_count": click_count}
            except Exception as exc:  # noqa: BLE001
                target = args.get("target") or {"selector": args.get("selector")}
                return {
                    "ok": False,
                    "error": f"Could not click target {target!r}: {exc}",
                    "click_count": click_count,
                    "next_step": "Observe the page and use a currently visible element index, role/name, label, placeholder, or text target.",
                }
        if name == "click_at":
            click_count = max(1, min(3, int(args.get("click_count", 1))))
            await page.mouse.click(args["x"], args["y"], click_count=click_count)
            return {"ok": True, "click_count": click_count}
        if name == "type_text":
            try:
                locator = await self.resolve_locator(page, args)
                await locator.scroll_into_view_if_needed(timeout=min(timeout, 10_000))
                try:
                    await locator.fill(args["text"], timeout=min(timeout, 10_000))
                except Exception:
                    await locator.click(timeout=min(timeout, 5_000))
                    await page.keyboard.press("ControlOrMeta+A")
                    await page.keyboard.insert_text(args["text"])
                return {"ok": True}
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": str(exc), "next_step": "Observe the page and use a structured target for the current field."}
        if name == "press_key":
            await page.keyboard.press(args["key"])
            return {"ok": True}
        if name == "select_option":
            try:
                locator = await self.resolve_locator(page, args)
                await locator.select_option(args["value"], timeout=min(timeout, 10_000))
                return {"ok": True}
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": str(exc), "next_step": "Observe the page and use a structured target for the current select."}
        if name == "drag":
            await page.mouse.move(args["start_x"], args["start_y"])
            await page.mouse.down()
            await page.mouse.move(args["end_x"], args["end_y"], steps=12)
            await page.mouse.up()
            return {"ok": True}
        if name == "scroll":
            await page.mouse.wheel(0, args["amount"])
            return {"ok": True}
        if name == "wait":
            milliseconds = max(250, min(10_000, int(args.get("milliseconds", 1_000))))
            await asyncio.sleep(milliseconds / 1000)
            return {"ok": True, "waited_ms": milliseconds}
        if name == "navigate":
            direction = args["direction"]
            if direction == "back":
                await page.go_back(wait_until="domcontentloaded", timeout=timeout)
            elif direction == "forward":
                await page.go_forward(wait_until="domcontentloaded", timeout=timeout)
            elif direction == "reload":
                await page.reload(wait_until="domcontentloaded", timeout=timeout)
            else:
                raise ValueError(f"Unsupported navigation direction: {direction}")
            return {"ok": True, "url": page.url}
        if name == "focus":
            locator = await self.resolve_locator(page, args)
            await locator.scroll_into_view_if_needed(timeout=min(timeout, 10_000))
            await locator.focus(timeout=min(timeout, 10_000))
            return {"ok": True}
        if name == "screenshot":
            safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", args["name"])[:80]
            path = session.artifact_dir / session.task_id / f"{safe_name}.png"
            path.parent.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=str(path), full_page=True)
            return {"ok": True, "artifact": f"/artifacts/{session.task_id}/{path.name}"}
        if name == "submit_form":
            if session.require_confirmation and not allow_submission:
                return {"ok": False, "confirmation_required": True}
            form = page.locator("form").first
            await form.evaluate("form => form.requestSubmit()")
            return {"ok": True, "submitted": True}
        if name == "finish":
            return {"ok": True, "summary": args.get("summary", "Finished")}
        raise ValueError(f"Unsupported agent tool: {name}")

    async def observe(self, page: Page) -> str:
        title = await page.title()
        body = (await page.locator("body").inner_text(timeout=5_000))[:20_000]
        controls = await page.locator("input, textarea, select, button, a, [role='button'], [role='link'], [role='textbox'], [role='combobox'], [role='checkbox'], [role='radio']").evaluate_all(
            """els => els.filter(el => {
                const style = window.getComputedStyle(el);
                return style.display !== 'none' && style.visibility !== 'hidden' && el.getBoundingClientRect().width > 0 && el.getBoundingClientRect().height > 0;
            }).slice(0, 100).map((el, i) => ({
                index: i, tag: el.tagName.toLowerCase(), text: (el.innerText || el.value || '').slice(0, 160),
                id: el.id || null, name: el.getAttribute('name'), type: el.getAttribute('type'), role: el.getAttribute('role'),
                placeholder: el.getAttribute('placeholder'), aria: el.getAttribute('aria-label'),
                disabled: el.disabled || false
            }))"""
        )
        canvases = await page.locator("canvas").evaluate_all("els => els.map((el, i) => ({index:i, width:el.width, height:el.height}))")
        forms = await page.locator("form").evaluate_all("els => els.slice(0, 20).map((el, i) => ({index:i, action:el.action || null, method:el.method || null}))")
        dialogs = await page.locator("[role='dialog'], [aria-modal='true']").evaluate_all("els => els.slice(0, 10).map(el => (el.innerText || '').slice(0, 1000))")
        frame_urls = [frame.url for frame in page.frames]
        observation = {"url": page.url, "title": title, "body_text": body, "controls": controls, "forms": forms, "dialogs": dialogs, "canvases": canvases, "frames": frame_urls}
        encoded = json.dumps(observation, ensure_ascii=False)
        observation_hash = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        observation["observation_hash"] = observation_hash
        return json.dumps(observation, ensure_ascii=False)

    @staticmethod
    def _mask_args(name: str, args: dict[str, Any]) -> dict[str, Any]:
        if name == "type_text" and "text" in args:
            return {**args, "text": "[REDACTED]"}
        return args
