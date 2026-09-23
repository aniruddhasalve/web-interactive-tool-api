from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from openai import AsyncOpenAI
from playwright.async_api import BrowserContext, Page

from .security import validate_public_url

MODEL = os.getenv("OPENAI_MODEL", "gpt-5-mini")

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "click",
            "description": "Click a visible element using a CSS selector.",
            "parameters": {"type": "object", "properties": {"selector": {"type": "string"}}, "required": ["selector"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "click_at",
            "description": "Click a viewport coordinate. Useful for canvas games when there is no DOM selector.",
            "parameters": {"type": "object", "properties": {"x": {"type": "number"}, "y": {"type": "number"}}, "required": ["x", "y"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "type_text",
            "description": "Fill a visible input, textarea, or contenteditable element.",
            "parameters": {"type": "object", "properties": {"selector": {"type": "string"}, "text": {"type": "string"}}, "required": ["selector", "text"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "press_key",
            "description": "Press a keyboard key such as Enter, Escape, ArrowLeft, or Space.",
            "parameters": {"type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "select_option",
            "description": "Select an option in a native HTML select element.",
            "parameters": {"type": "object", "properties": {"selector": {"type": "string"}, "value": {"type": "string"}}, "required": ["selector", "value"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "drag",
            "description": "Drag from one viewport coordinate to another. Useful for canvas games and drawing applications.",
            "parameters": {"type": "object", "properties": {"start_x": {"type": "number"}, "start_y": {"type": "number"}, "end_x": {"type": "number"}, "end_y": {"type": "number"}}, "required": ["start_x", "start_y", "end_x", "end_y"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "scroll",
            "description": "Scroll the page vertically.",
            "parameters": {"type": "object", "properties": {"amount": {"type": "integer", "minimum": -2000, "maximum": 2000}}, "required": ["amount"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "screenshot",
            "description": "Save a screenshot of the current page state.",
            "parameters": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "submit_form",
            "description": "Submit the primary form. This always pauses for user confirmation first.",
            "parameters": {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish",
            "description": "Finish when the user request is complete or cannot be completed.",
            "parameters": {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]},
        },
    },
]

SYSTEM_PROMPT = """You are a browser agent. Complete the user's instruction by observing the current page and using only the provided browser tools.

Rules:
- Never invent that an action succeeded; inspect the page after actions.
- Prefer visible, public UI controls. Do not bypass CAPTCHAs, authentication, paywalls, or access controls.
- For games and canvas apps, use drag, click, and press_key based on the observed canvas dimensions and visible UI.
- Do not submit forms, make purchases, send messages, or change account/security settings without the submit_form tool; it is confirmation-gated.
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
    step_count: int = 0
    pending_confirmation: dict[str, Any] | None = None
    events: list[dict[str, Any]] = field(default_factory=list)


class AgentRunner:
    def __init__(self, browser, artifact_dir: Path) -> None:
        self.browser = browser
        self.artifact_dir = artifact_dir
        api_key = os.getenv("OPENAI_API_KEY")
        base_url = os.getenv("OPENAI_BASE_URL") or os.getenv("OPENAI_API_BASE")
        self.client = AsyncOpenAI(api_key=api_key, base_url=base_url) if api_key else None

    async def start_session(self, task_id: str, url: str, instruction: str, max_steps: int, timeout_seconds: int) -> AgentSession:
        if not self.client:
            raise RuntimeError("OPENAI_API_KEY is not configured")
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
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"User instruction: {instruction}\nInitial page observation:\n{await self.observe(page)}"},
            ],
            max_steps=max_steps,
            timeout_seconds=timeout_seconds,
            artifact_dir=self.artifact_dir,
        )
        return session

    async def run_until_pause(self, session: AgentSession, allow_submission: bool = False) -> dict[str, Any]:
        if not self.client:
            raise RuntimeError("OPENAI_API_KEY is not configured")
        while session.step_count < session.max_steps:
            session.step_count += 1
            completion = await self.client.chat.completions.create(
                model=MODEL,
                messages=session.messages,
                tools=TOOLS,
                tool_choice="auto",
                max_completion_tokens=1_500,
            )
            if not completion.choices:
                raise RuntimeError("The model returned no choices")
            message = completion.choices[0].message
            session.messages.append(message.model_dump(exclude_none=True))
            if not message.tool_calls:
                summary = message.content or "The agent stopped without a summary."
                return {"status": "completed", "summary": summary}

            for tool_call in message.tool_calls:
                name = tool_call.function.name
                args = json.loads(tool_call.function.arguments or "{}")
                event = {"step": session.step_count, "tool": name, "arguments": self._mask_args(name, args)}
                if name == "submit_form" and not allow_submission:
                    session.pending_confirmation = {
                        "reason": args.get("reason", "The agent wants to submit the form."),
                        "url": session.page.url,
                        "tool_call_id": tool_call.id,
                    }
                    event["status"] = "waiting_confirmation"
                    session.events.append(event)
                    return {"status": "waiting_confirmation", "confirmation": session.pending_confirmation}

                result = await self.execute_tool(session, name, args, allow_submission)
                event["result"] = result
                session.events.append(event)
                session.messages.append({"role": "tool", "tool_call_id": tool_call.id, "content": json.dumps(result)})

            session.messages.append({"role": "user", "content": f"Updated page observation after step {session.step_count}:\n{await self.observe(session.page)}"})

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
            if not allow_submission:
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
