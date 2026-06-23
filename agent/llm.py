"""The LLM-vision agent (optional).

This brain implements the "AI-driven browser control" idea literally: Claude is
given a screenshot of the page and a set of tools matching the agent's
contract, and it decides — turn by turn — where to click, what to type, and
when the task is done. The harness executes each requested tool, captures a
fresh screenshot, and feeds it back as the tool result, closing the
perceive → decide → act loop.

It requires ``ANTHROPIC_API_KEY``. The deterministic agent needs no key and is
the safer choice for a live demo; this mode is the more impressive one to show
*how* an autonomous agent reasons over pixels.

Why the 1280x800 viewport matters: it keeps each screenshot under ~1.15 MP, so
Claude sees the image at native resolution and the (x, y) coordinates it
returns map 1:1 to real page pixels — no scaling math required.
"""

from __future__ import annotations

import base64

from .config import Config
from .logger import get_logger
from .tools import BrowserTools

log = get_logger("llm")

# Tool definitions handed to Claude. Names mirror the BrowserTools methods.
_TOOLS = [
    {
        "name": "click_on_screen",
        "description": "Click the mouse at pixel coordinates (x, y), measured "
        "from the top-left of the screenshot. Use this to focus an input field "
        "or press a button.",
        "input_schema": {
            "type": "object",
            "properties": {
                "x": {"type": "number", "description": "Horizontal pixel coordinate."},
                "y": {"type": "number", "description": "Vertical pixel coordinate."},
            },
            "required": ["x", "y"],
        },
    },
    {
        "name": "double_click",
        "description": "Double-click at pixel coordinates (x, y). Useful to "
        "focus a field and select a word.",
        "input_schema": {
            "type": "object",
            "properties": {
                "x": {"type": "number"},
                "y": {"type": "number"},
            },
            "required": ["x", "y"],
        },
    },
    {
        "name": "send_keys",
        "description": "Type text into the currently focused field. Click the "
        "field first to focus it.",
        "input_schema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
    {
        "name": "scroll",
        "description": "Scroll the page vertically to reveal hidden elements.",
        "input_schema": {
            "type": "object",
            "properties": {
                "direction": {"type": "string", "enum": ["up", "down"]},
                "amount": {
                    "type": "integer",
                    "description": "Pixels to scroll (optional; defaults to ~one screen).",
                },
            },
            "required": ["direction"],
        },
    },
    {
        "name": "take_screenshot",
        "description": "Capture the current screen again — use if you are unsure "
        "of the page state.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "report_done",
        "description": "Call this when the Name/Title and Description fields are "
        "both filled in. Provide a short summary of what you did.",
        "input_schema": {
            "type": "object",
            "properties": {"summary": {"type": "string"}},
            "required": ["summary"],
        },
    },
]

_SYSTEM_TEMPLATE = """You are AgentFlow, an autonomous web-automation agent controlling a real browser.

The viewport is {width}x{height} pixels. You see it through screenshots; all \
coordinates you provide are pixels from the top-left of the screenshot and map \
1:1 to the page.

YOUR TASK on the current page:
1. Find the first text field (it may be labelled "Name", "Title", or "Bug \
Title") and fill it with exactly: "{name_value}"
2. Find the multi-line "Description" field and fill it with exactly: \
"{description_value}"
{submit_clause}

HOW TO WORK:
- To fill a field: click it to focus it, then use send_keys to type the value.
- Work one action at a time; after each action you receive a fresh screenshot.
- Ignore the page's search box and navigation — only the form matters.
- When both fields contain the requested text, call report_done with a summary.
Be efficient and deliberate."""


class LLMAgent:
    """Drives the browser by asking Claude what to do from screenshots."""

    def __init__(self, tools: BrowserTools, config: Config) -> None:
        self.tools = tools
        self.config = config
        if not config.anthropic_api_key:
            raise RuntimeError(
                "LLM mode requires ANTHROPIC_API_KEY. Set it in your environment "
                "or .env file, or use the deterministic agent (the default)."
            )
        # Imported lazily so the deterministic path never needs the SDK.
        import anthropic

        self.client = anthropic.Anthropic()

    def run(
        self,
        url: str,
        name_value: str,
        description_value: str,
        submit: bool = False,
    ) -> None:
        log.info("=== LLM-vision agent starting (model=%s) ===", self.config.model)
        self.tools.open_browser()
        self.tools.navigate_to_url(url)

        submit_clause = (
            "3. Click the form's Submit button.\n" if submit else ""
        )
        system = _SYSTEM_TEMPLATE.format(
            width=self.config.viewport_width,
            height=self.config.viewport_height,
            name_value=name_value,
            description_value=description_value,
            submit_clause=submit_clause,
        )

        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Here is the current screen. Begin the task."},
                    self._screenshot_block("llm-00-initial"),
                ],
            }
        ]

        for step in range(1, self.config.max_steps + 1):
            log.info("--- LLM step %d/%d ---", step, self.config.max_steps)
            response = self.client.messages.create(
                model=self.config.model,
                # Headroom for adaptive thinking tokens *plus* the tool_use block;
                # 4096 risks hitting the cap mid-thought and stalling the loop.
                max_tokens=8192,
                thinking={"type": "adaptive"},
                system=system,
                tools=_TOOLS,
                tool_choice={"type": "auto", "disable_parallel_tool_use": True},
                messages=messages,
            )
            # Preserve the full assistant turn (incl. thinking blocks) verbatim.
            messages.append({"role": "assistant", "content": response.content})

            for block in response.content:
                if block.type == "text" and block.text.strip():
                    log.info("LLM says: %s", block.text.strip())

            if response.stop_reason != "tool_use":
                if response.stop_reason == "max_tokens":
                    log.warning(
                        "Hit max_tokens before a tool call — raise max_tokens or "
                        "lower effort. Ending loop."
                    )
                else:
                    log.info(
                        "LLM ended without a tool call (stop_reason=%s).",
                        response.stop_reason,
                    )
                break

            tool_results = []
            done = False
            for block in response.content:
                if block.type != "tool_use":
                    continue
                output, attach_shot = self._execute(block.name, block.input, step)
                content: list = []
                if output:
                    content.append({"type": "text", "text": output})
                if attach_shot:
                    content.append({"type": "text", "text": "Screen after the action:"})
                    content.append(self._screenshot_block(f"llm-{step:02d}-{block.name}"))
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": content,
                    }
                )
                if block.name == "report_done":
                    done = True

            messages.append({"role": "user", "content": tool_results})
            if done:
                log.info("LLM reported the task complete.")
                break
        else:
            log.warning("Reached max steps (%d) without report_done.", self.config.max_steps)

        self.tools.take_screenshot("llm-final")
        log.info("=== LLM-vision agent finished ===")

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _screenshot_block(self, name: str) -> dict:
        _, data = self.tools.take_screenshot(name)
        b64 = base64.standard_b64encode(data).decode("ascii")
        return {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": b64},
        }

    def _execute(self, name: str, args: dict, step: int) -> tuple[str, bool]:
        """Run a tool Claude requested. Returns (text_result, attach_screenshot)."""
        if name == "click_on_screen":
            self.tools.click_on_screen(args["x"], args["y"])
            return f"Clicked at ({args['x']:.0f}, {args['y']:.0f}).", True
        if name == "double_click":
            self.tools.double_click(args["x"], args["y"])
            return f"Double-clicked at ({args['x']:.0f}, {args['y']:.0f}).", True
        if name == "send_keys":
            self.tools.send_keys(args["text"])
            return f"Typed: {args['text']!r}", True
        if name == "scroll":
            self.tools.scroll(args.get("direction", "down"), args.get("amount"))
            return "Scrolled.", True
        if name == "take_screenshot":
            return "", True
        if name == "report_done":
            return f"Acknowledged. Summary: {args.get('summary', '')}", False
        return f"Unknown tool {name!r}.", False
