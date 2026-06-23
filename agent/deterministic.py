"""The deterministic (rule-based) agent.

This brain always works without an API key and is the recommended path for a
live demo. It composes the browser tools into the target workflow:

    open_browser -> navigate_to_url -> take_screenshot -> detect fields
    -> scroll into view -> click/double-click -> send_keys -> verify -> screenshot

Element *detection* is delegated to :class:`ElementDetector`; this module owns
the *orchestration* and the coordinate-based interaction. Clicks go through
``click_on_screen(x, y)`` / ``double_click(x, y)`` using the centre of each
field's bounding box, so the agent genuinely drives the page by pixel
coordinates while remaining reliable.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import Config
from .detector import DetectionResult, ElementDetector
from .logger import get_logger
from .tools import BrowserTools

log = get_logger("deterministic")


@dataclass
class RunResult:
    """Summary of a run, returned to the caller for reporting / exit codes."""

    success: bool
    name_filled: bool = False
    description_filled: bool = False
    submitted: bool = False
    message: str = ""


class DeterministicAgent:
    """Rule-based agent that fills the Name/Title and Description fields."""

    def __init__(self, tools: BrowserTools, config: Config) -> None:
        self.tools = tools
        self.config = config
        self.detector = ElementDetector(tools)

    def run(
        self,
        url: str,
        name_value: str,
        description_value: str,
        submit: bool = False,
    ) -> RunResult:
        log.info("=== Deterministic agent starting ===")

        # 1. Launch + navigate.
        self.tools.open_browser()
        self.tools.navigate_to_url(url)
        self.tools.take_screenshot("01-initial")

        # 2. Intelligently locate the form fields.
        detection = self.detector.detect_form_fields()
        if not detection:
            self.tools.take_screenshot("error-no-fields")
            return RunResult(False, message="No form fields detected.")

        result = RunResult(success=True)

        # 3. Fill the Name/Title field with a single click.
        if detection.name:
            self._fill_field(detection, "name", name_value, use_double_click=False)
            result.name_filled = self._verify(detection, "name", name_value)
        else:
            log.warning("No Name/Title field found; skipping.")

        # 4. Fill the Description field — demonstrate double_click for focus.
        if detection.desc:
            self._fill_field(detection, "desc", description_value, use_double_click=True)
            result.description_filled = self._verify(detection, "desc", description_value)
        else:
            log.warning("No Description field found; skipping.")

        self.tools.take_screenshot("02-filled")

        # 5. Optionally submit.
        if submit:
            result.submitted = self._submit(detection)
            self.tools.take_screenshot("03-submitted")

        result.success = result.name_filled or result.description_filled
        log.info(
            "=== Done. name_filled=%s description_filled=%s submitted=%s ===",
            result.name_filled,
            result.description_filled,
            result.submitted,
        )
        return result

    # ── Steps ────────────────────────────────────────────────────────────────

    def _fill_field(
        self,
        detection: DetectionResult,
        tag: str,
        value: str,
        use_double_click: bool,
    ) -> None:
        frame = detection.frame

        # Demonstrate the scroll tool: if the field sits low in (or below) the
        # viewport, scroll it toward the upper third before interacting.
        top = self.detector.field_top(frame, tag)
        if top is not None:
            vh = self.config.viewport_height
            if top > vh * 0.6 or top < 60:
                delta = int(top - vh * 0.35)
                self.tools.scroll(
                    direction="down" if delta > 0 else "up", amount=abs(delta)
                )

        # Compute the click target (scrolls precisely into view, then measures).
        cx, cy = self.detector.click_target(frame, tag)

        # Focus the field by clicking at its centre.
        if use_double_click:
            self.tools.double_click(cx, cy)
        else:
            self.tools.click_on_screen(cx, cy)

        # If the field already holds text, select-all so we overwrite cleanly.
        if self.detector.field_value(frame, tag):
            self.tools.press_key("ControlOrMeta+A")

        # Type the value.
        self.tools.send_keys(value)

    def _verify(self, detection: DetectionResult, tag: str, expected: str) -> bool:
        actual = self.detector.field_value(detection.frame, tag)
        ok = actual.strip() == expected.strip()
        log.info(
            "Verify %s field: %s (value=%r)",
            tag,
            "OK" if ok else "MISMATCH",
            actual,
        )
        return ok

    def _submit(self, detection: DetectionResult) -> bool:
        """Click the form's submit button via its centre coordinates."""
        frame = detection.frame
        button = frame.locator(
            "form button[type=submit], "
            "form button:has-text('Submit'), "
            "form button:has-text('Save')"
        ).first
        try:
            button.scroll_into_view_if_needed()
            box = button.bounding_box()
            if not box:
                log.warning("Submit button not measurable; skipping submit.")
                return False
            self.tools.click_on_screen(
                box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
            )
            log.info("Clicked submit button.")
            return True
        except Exception as exc:
            log.warning("Could not submit: %s", exc)
            return False
