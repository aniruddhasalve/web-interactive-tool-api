from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from playwright.async_api import Browser, Page, async_playwright

from .models import Action, SiteResult, SiteTarget
from .security import validate_public_url


class BrowserRunner:
    def __init__(self, artifact_dir: Path) -> None:
        self.artifact_dir = artifact_dir
        self._playwright = None
        self._browser: Browser | None = None

    async def start(self) -> None:
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=True,
            args=["--disable-dev-shm-usage", "--no-sandbox"],
        )

    async def stop(self) -> None:
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()

    async def run_site(
        self,
        job_id: str,
        target: SiteTarget,
        timeout_seconds: int,
    ) -> SiteResult:
        started = time.perf_counter()
        url = str(target.url)
        try:
            validate_public_url(url)
            if not self._browser:
                raise RuntimeError("Browser is not started")

            context = await self._browser.new_context(
                viewport={"width": 1440, "height": 900},
                ignore_https_errors=False,
            )
            page = await context.new_page()
            page.set_default_timeout(timeout_seconds * 1000)
            response = await page.goto(url, wait_until="domcontentloaded", timeout=timeout_seconds * 1000)
            extracted: list[dict[str, Any]] = []
            screenshot_path: str | None = None
            page_title: str | None = None

            for action in target.actions:
                value = await self._run_action(page, action, job_id, target.name)
                if action.type == "extract_title":
                    page_title = value
                elif action.type == "screenshot":
                    screenshot_path = value
                elif value is not None:
                    extracted.append({"action": action.type, "value": value})

            if page_title is None:
                page_title = await page.title()

            await context.close()
            return SiteResult(
                site=target.name,
                url=url,
                success=True,
                status_code=response.status if response else None,
                page_title=page_title,
                extracted=extracted,
                screenshot=screenshot_path,
                duration_ms=round((time.perf_counter() - started) * 1000),
            )
        except Exception as exc:  # Per-site failures should not cancel the rest of the job.
            return SiteResult(
                site=target.name,
                url=url,
                success=False,
                duration_ms=round((time.perf_counter() - started) * 1000),
                error=str(exc)[:500],
            )

    async def _run_action(self, page: Page, action: Action, job_id: str, site_name: str) -> Any:
        if action.type == "wait_for_load":
            await page.wait_for_load_state("domcontentloaded", timeout=action.timeout_ms)
        elif action.type == "wait_for_selector":
            if not action.selector:
                raise ValueError("wait_for_selector requires selector")
            await page.wait_for_selector(action.selector, timeout=action.timeout_ms)
        elif action.type == "extract_title":
            return await page.title()
        elif action.type == "extract_text":
            if not action.selector:
                raise ValueError("extract_text requires selector")
            return await page.locator(action.selector).inner_text(timeout=action.timeout_ms)
        elif action.type == "click":
            if not action.selector:
                raise ValueError("click requires selector")
            await page.locator(action.selector).first.click(timeout=action.timeout_ms)
        elif action.type == "fill":
            if not action.selector or action.text is None:
                raise ValueError("fill requires selector and text")
            await page.locator(action.selector).first.fill(action.text, timeout=action.timeout_ms)
        elif action.type == "screenshot":
            safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", site_name)
            path = self.artifact_dir / job_id / f"{safe_name}.png"
            path.parent.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=str(path), full_page=action.full_page)
            return f"/artifacts/{job_id}/{path.name}"
        return None
