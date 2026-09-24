from __future__ import annotations

import asyncio
import json
import os
import re
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
        "description": "Click a visible element using a CSS selector.",
        "parameters": {"type": "object", "properties": {"selector": {"type": "string"}}, "required": ["selector"]},
    },
    {
        "name": "click_at",
        "description": "Click a viewport coordinate. Useful for canvas games when there is no DOM selector.",
        "parameters": {"type": "object", "properties": {"x": {"type": "number"}, "y": {"type": "number"}}, "required": ["x", "y"]},
    },
    {
        "name": "type_text",
        "description": "Fill a visible input, textarea, or contenteditable element.",
        "parameters": {"type": "object", "properties": {"selector": {"type": "string"}, "text": {"type": "string"}}, "required": ["selector", "text"]},
    },
    {
        "name": "press_key",
        "description": "Press a keyboard key such as Enter, Escape, ArrowLeft, or Space.",
        "parameters": {"type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"]},
    },
    {
        "name": "select_option",
        "description": "Select an option in a native HTML select element.",
        "parameters": {"type": "object", "properties": {"selector": {"type": "string"}, "value": {"type": "string"}}, "required": ["selector", "value"]},
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
- Prefer visible, public UI controls. Do not bypass CAPTCHAs, authentication, paywalls, or access controls.
- For games and canvas apps, use drag, click, and press_key based on the observed canvas dimensions and visible UI.
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
        context = await self.browser.new_context(viewport={"width": 1440, "height": 900})
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
            completion = await asyncio.to_thread(
                self.client.converse,
                modelId=MODEL_ID,
                system=[{"text": SYSTEM_PROMPT}],
                messages=session.messages,
                toolConfig={"tools": BEDROCK_TOOLS},
                inferenceConfig={"maxTokens": 1_500},
            )
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

                result = await self.execute_tool(session, name, args, allow_submission)
                event["result"] = result
                session.events.append(event)
                tool_results.append({
                    "toolResult": {
                        "toolUseId": tool_call["toolUseId"],
                        "content": [{"text": json.dumps(result)}],
                    }
                })

            session.messages.append({"role": "user", "content": tool_results})
            session.messages.append({
                "role": "user",
                "content": [{"text": f"Updated page observation after step {session.step_count}:\n{await self.observe(session.page)}"}],
            })

        return {"status": "failed", "error": f"Agent reached the maximum of {session.max_steps} steps"}

    async def execute_tool(self, session: AgentSession, name: str, args: dict[str, Any], allow_submission: bool) -> dict[str, Any]:
        page = session.page
        timeout = session.timeout_seconds * 1000
        if name == "click":
            await page.locator(args["selector"]).first.click(timeout=timeout)
            return {"ok": True}
        if name == "click_at":
            await page.mouse.click(args["x"], args["y"])
            return {"ok": True}
        if name == "type_text":
            await page.locator(args["selector"]).first.fill(args["text"], timeout=timeout)
            return {"ok": True}
        if name == "press_key":
            await page.keyboard.press(args["key"])
            return {"ok": True}
        if name == "select_option":
            await page.locator(args["selector"]).first.select_option(args["value"], timeout=timeout)
            return {"ok": True}
        if name == "drag":
            await page.mouse.move(args["start_x"], args["start_y"])
            await page.mouse.down()
            await page.mouse.move(args["end_x"], args["end_y"], steps=12)
            await page.mouse.up()
            return {"ok": True}
        if name == "scroll":
            await page.mouse.wheel(0, args["amount"])
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
        body = (await page.locator("body").inner_text(timeout=5_000))[:12_000]
        controls = await page.locator("input, textarea, select, button, a").evaluate_all(
            """els => els.slice(0, 100).map((el, i) => ({
                index: i, tag: el.tagName.toLowerCase(), text: (el.innerText || el.value || '').slice(0, 160),
                id: el.id || null, name: el.getAttribute('name'), type: el.getAttribute('type'),
                placeholder: el.getAttribute('placeholder'), aria: el.getAttribute('aria-label')
            }))"""
        )
        canvases = await page.locator("canvas").evaluate_all("els => els.map((el, i) => ({index:i, width:el.width, height:el.height}))")
        return json.dumps({"url": page.url, "title": title, "body_text": body, "controls": controls, "canvases": canvases}, ensure_ascii=False)

    @staticmethod
    def _mask_args(name: str, args: dict[str, Any]) -> dict[str, Any]:
        if name == "type_text" and "text" in args:
            return {**args, "text": "[REDACTED]"}
        return args
