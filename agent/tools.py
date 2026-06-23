"""The AgentFlow tool belt.

A thin, composable wrapper around Playwright that exposes exactly the
capabilities the assignment requires, each as a single method:

    * open_browser()          - launch a browser instance
    * navigate_to_url(url)    - point the browser at a URL
    * take_screenshot(name)   - capture the current viewport
    * click_on_screen(x, y)   - click at pixel coordinates
    * double_click(x, y)      - double-click at pixel coordinates
    * send_keys(text)         - type text into the focused element
    * scroll(direction, ...)  - scroll the page

Every method logs its invocation so a run produces a complete, auditable trace.
Clicks are intentionally coordinate-based (via ``page.mouse``) rather than
selector-based, matching the ``click_on_screen(x, y)`` contract — the agents
compute those coordinates from element bounding boxes (deterministic mode) or
from screenshots (LLM mode).
"""

from __future__ import annotations

import time
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

from .config import Config
from .logger import get_logger

log = get_logger("tools")


class BrowserError(RuntimeError):
    """Raised when a tool is used before the browser is ready, etc."""


class BrowserTools:
    """Stateful holder for a Playwright session + the agent's tool methods."""

    def __init__(self, config: Config | None = None) -> None:
        self.config = config or Config()
        self._playwright = None
        self._browser = None
        self._context = None
        self.page: Page | None = None
        self.config.screenshot_dir.mkdir(parents=True, exist_ok=True)

    # ── Lifecycle ──────────────────────────────────────────────────────────

    def open_browser(self) -> Page:
        """Launch a browser instance and open a fresh page.

        Honours the configured engine (chromium/firefox/webkit), headless flag,
        slow-mo, and viewport size.
        """
        log.info(
            "TOOL open_browser(engine=%s, headless=%s, viewport=%dx%d)",
            self.config.browser,
            self.config.headless,
            self.config.viewport_width,
            self.config.viewport_height,
        )
        self._playwright = sync_playwright().start()
        try:
            engine = getattr(self._playwright, self.config.browser)
        except AttributeError as exc:  # invalid AGENTFLOW_BROWSER
            raise BrowserError(
                f"Unknown browser engine {self.config.browser!r}; "
                "use chromium, firefox, or webkit."
            ) from exc

        self._browser = engine.launch(
            headless=self.config.headless,
            slow_mo=self.config.slow_mo,
        )
        self._context = self._browser.new_context(
            viewport={
                "width": self.config.viewport_width,
                "height": self.config.viewport_height,
            }
        )
        self.page = self._context.new_page()
        self.page.set_default_timeout(self.config.timeout_ms)
        return self.page

    def close(self) -> None:
        """Tear down the page, context, browser, and Playwright driver."""
        log.info("TOOL close()")
        for closer in (
            lambda: self._context and self._context.close(),
            lambda: self._browser and self._browser.close(),
            lambda: self._playwright and self._playwright.stop(),
        ):
            try:
                closer()
            except Exception as exc:  # best-effort cleanup
                log.debug("Cleanup step failed: %s", exc)
        self.page = self._context = self._browser = self._playwright = None

    # ── Navigation ─────────────────────────────────────────────────────────

    def navigate_to_url(self, url: str) -> str:
        """Navigate to ``url`` and wait for the page to settle."""
        self._require_page()
        log.info("TOOL navigate_to_url(%s)", url)
        self.page.goto(url, wait_until="domcontentloaded")
        # The shadcn docs hydrate React after DOM load; give the network a
        # moment to go idle so form components are interactive. This is
        # best-effort: a busy analytics beacon shouldn't block the run.
        try:
            self.page.wait_for_load_state("networkidle", timeout=self.config.timeout_ms)
        except Exception:
            log.debug("networkidle wait timed out; continuing anyway")
        log.info("Loaded: %s", self.page.url)
        return self.page.url

    # ── Observation ──────────────────────────────────────────────────────────

    def take_screenshot(self, name: str | None = None) -> tuple[Path, bytes]:
        """Capture the current viewport.

        Returns ``(path, png_bytes)`` — the path for humans, the bytes for the
        LLM agent to embed as a vision input.
        """
        self._require_page()
        if not name:
            name = f"screenshot-{int(time.time() * 1000)}"
        if not name.endswith(".png"):
            name += ".png"
        path = self.config.screenshot_dir / name
        data = self.page.screenshot(path=str(path), full_page=False)
        log.info("TOOL take_screenshot -> %s", path.name)
        return path, data

    # ── Interaction ──────────────────────────────────────────────────────────

    def click_on_screen(self, x: float, y: float) -> None:
        """Click at absolute pixel coordinates within the viewport."""
        self._require_page()
        log.info("TOOL click_on_screen(x=%.0f, y=%.0f)", x, y)
        self.page.mouse.move(x, y)
        self.page.mouse.click(x, y)

    def double_click(self, x: float, y: float) -> None:
        """Double-click at absolute pixel coordinates within the viewport."""
        self._require_page()
        log.info("TOOL double_click(x=%.0f, y=%.0f)", x, y)
        self.page.mouse.move(x, y)
        self.page.mouse.dblclick(x, y)

    def send_keys(self, text: str) -> None:
        """Type ``text`` into the currently focused element, key by key."""
        self._require_page()
        log.info("TOOL send_keys(%r)", text)
        self.page.keyboard.type(text, delay=self.config.type_delay_ms)

    def press_key(self, key: str) -> None:
        """Press a single key or chord (e.g. 'Enter', 'ControlOrMeta+A')."""
        self._require_page()
        log.info("TOOL press_key(%s)", key)
        self.page.keyboard.press(key)

    def scroll(self, direction: str = "down", amount: int | None = None) -> None:
        """Scroll the page vertically.

        ``direction`` is "down" or "up"; ``amount`` is pixels (defaults to ~80%
        of the viewport height, a natural "page down").
        """
        self._require_page()
        if amount is None:
            amount = int(self.config.viewport_height * 0.8)
        delta = amount if direction == "down" else -amount
        log.info("TOOL scroll(direction=%s, amount=%d)", direction, amount)
        self.page.mouse.wheel(0, delta)

    # ── Internal ─────────────────────────────────────────────────────────────

    def _require_page(self) -> None:
        if self.page is None:
            raise BrowserError("Browser not open. Call open_browser() first.")
