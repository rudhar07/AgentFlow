"""The Gemini-vision agent (optional).

A second "AI-driven browser control" brain, parallel to the Claude one in
``llm.py`` — but backed by **Google Gemini** via the ``google-genai`` SDK. Gemini
is shown a screenshot of the page plus a set of tools (matching the agent's
contract), and it decides — turn by turn — where to click, what to type, and
when the task is done. The harness executes each requested tool, captures a
fresh screenshot, and feeds it back, closing the perceive → decide → act loop.

Unlike the deterministic agent, this one accepts a **free-form ``task``** — so it
can attempt arbitrary goals (e.g. "open YouTube and search for lofi music"),
not just the Name/Description form fill.

Requires a Gemini API key (``GEMINI_API_KEY`` or ``GOOGLE_API_KEY``). The
deterministic agent needs no key and is the reliable choice for a demo; this
mode is the open-ended, more impressive one.

SDK notes (verified against the current ``google-genai``):
    * client:   ``genai.Client(api_key=...)``
    * image:    ``types.Part.from_bytes(data=<png bytes>, mime_type="image/png")``
    * tools:    ``types.Tool(function_declarations=[types.FunctionDeclaration(...)])``
                passed via ``types.GenerateContentConfig(tools=[...])`` with
                automatic function calling DISABLED so we drive the loop.
    * results:  ``types.Part.from_function_response(name=..., response={...})``
"""

from __future__ import annotations

import time

from .config import Config
from .logger import get_logger
from .tools import BrowserTools

log = get_logger("gemini")

_SYSTEM_FORM = """You are AgentFlow, an autonomous web-automation agent driving a real browser through tools.

The viewport is {w}x{h} pixels. You are given a screenshot each turn; every coordinate \
you pass is in pixels measured from the top-left of that screenshot (x in 0..{w}, y in 0..{h}).

YOUR TASK on the current page:
1. Find the first text field (it may be labelled "Name", "Title", or "Bug Title") and \
fill it with exactly: "{name}"
2. Find the multi-line "Description" field and fill it with exactly: "{description}"
{submit_clause}
HOW TO WORK:
- To fill a field: call click_on_screen at its centre to focus it, then send_keys to type.
- One action per turn; after each you receive a fresh screenshot.
- Ignore the page's search box and navigation — only the form matters.
- When the fields are filled (and submitted if asked), call report_done."""

_SYSTEM_TASK = """You are AgentFlow, an autonomous web-automation agent driving a real browser through tools.

The viewport is {w}x{h} pixels. You are given a screenshot each turn; every coordinate \
you pass is in pixels measured from the top-left of that screenshot (x in 0..{w}, y in 0..{h}).

YOUR TASK: {task}

HOW TO WORK:
- Use click_on_screen / double_click to click, send_keys to type (click a field first to \
focus it), and scroll to reveal content lower on the page.
- One action per turn; after each you receive a fresh screenshot showing the result.
- Be efficient and deliberate. When the task is complete, call report_done with a short summary."""


class GeminiAgent:
    """Drives the browser by asking Gemini what to do from screenshots."""

    def __init__(self, tools: BrowserTools, config: Config) -> None:
        self.tools = tools
        self.config = config
        if not config.gemini_api_key:
            raise RuntimeError(
                "Gemini mode requires a key: set GEMINI_API_KEY (or GOOGLE_API_KEY) "
                "in your environment or .env, or use the deterministic agent (the default)."
            )
        # Imported lazily so the deterministic / Claude paths never need this SDK.
        from google import genai
        from google.genai import types

        self._types = types
        self.client = genai.Client(api_key=config.gemini_api_key)

    # ── Public entry point ───────────────────────────────────────────────────

    def run(
        self,
        url: str,
        name_value: str,
        description_value: str,
        submit: bool = False,
        task: str | None = None,
    ) -> None:
        types = self._types
        log.info("=== Gemini-vision agent starting (model=%s) ===", self.config.gemini_model)
        self.tools.open_browser()
        self.tools.navigate_to_url(url)

        if task:
            system = _SYSTEM_TASK.format(
                w=self.config.viewport_width, h=self.config.viewport_height, task=task
            )
        else:
            submit_clause = "3. Click the form's Submit button.\n" if submit else ""
            system = _SYSTEM_FORM.format(
                w=self.config.viewport_width,
                h=self.config.viewport_height,
                name=name_value,
                description=description_value,
                submit_clause=submit_clause,
            )

        gen_config = types.GenerateContentConfig(
            system_instruction=system,
            tools=self._tool_declarations(),
            # Disable auto function calling so WE execute tools and inject a
            # fresh screenshot between turns.
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )

        contents = [
            types.Content(
                role="user",
                parts=[
                    types.Part(text="Here is the current screen. Begin the task."),
                    self._shot_part("gemini-00-initial"),
                ],
            )
        ]

        for step in range(1, self.config.max_steps + 1):
            log.info("--- Gemini step %d/%d ---", step, self.config.max_steps)
            response = self._generate(contents, gen_config)

            text = self._response_text(response)
            if text:
                log.info("Gemini says: %s", text)

            calls = response.function_calls
            if not calls:
                log.info("Gemini stopped without requesting a tool; ending loop.")
                break

            # Preserve the model's own turn (the function-call content) verbatim.
            contents.append(response.candidates[0].content)

            if any(fc.name == "report_done" for fc in calls):
                summary = next(
                    (dict(fc.args or {}).get("summary", "") for fc in calls if fc.name == "report_done"),
                    "",
                )
                log.info("Gemini reported done: %s", summary)
                break

            tool_parts = []
            for fc in calls:
                output = self._execute(fc.name, dict(fc.args or {}))
                tool_parts.append(
                    types.Part.from_function_response(
                        name=fc.name, response={"result": output}
                    )
                )
            # A fresh screenshot rides along with the tool results so Gemini sees
            # the new page state on its next turn.
            tool_parts.append(self._shot_part(f"gemini-{step:02d}"))
            contents.append(types.Content(role="tool", parts=tool_parts))
        else:
            log.warning("Reached max steps (%d).", self.config.max_steps)

        self.tools.take_screenshot("gemini-final")
        log.info("=== Gemini-vision agent finished ===")

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _tool_declarations(self) -> list:
        types = self._types
        fd = types.FunctionDeclaration
        xy = {
            "type": "object",
            "properties": {
                "x": {"type": "integer", "description": "X pixel coordinate"},
                "y": {"type": "integer", "description": "Y pixel coordinate"},
            },
            "required": ["x", "y"],
        }
        return [
            types.Tool(
                function_declarations=[
                    fd(
                        name="click_on_screen",
                        description="Click at pixel coordinates (x, y) from the top-left of the screenshot.",
                        parameters_json_schema=xy,
                    ),
                    fd(
                        name="double_click",
                        description="Double-click at pixel coordinates (x, y).",
                        parameters_json_schema=xy,
                    ),
                    fd(
                        name="send_keys",
                        description="Type text into the currently focused field.",
                        parameters_json_schema={
                            "type": "object",
                            "properties": {"text": {"type": "string"}},
                            "required": ["text"],
                        },
                    ),
                    fd(
                        name="scroll",
                        description="Scroll the page vertically to reveal hidden elements.",
                        parameters_json_schema={
                            "type": "object",
                            "properties": {
                                "direction": {"type": "string", "enum": ["up", "down"]},
                                "amount": {"type": "integer", "description": "Pixels (optional)"},
                            },
                            "required": ["direction"],
                        },
                    ),
                    fd(
                        name="take_screenshot",
                        description="Capture the current screen again if unsure of the state.",
                        parameters_json_schema={"type": "object", "properties": {}},
                    ),
                    fd(
                        name="report_done",
                        description="Call when the task is complete. Provide a short summary.",
                        parameters_json_schema={
                            "type": "object",
                            "properties": {"summary": {"type": "string"}},
                            "required": ["summary"],
                        },
                    ),
                ]
            )
        ]

    def _generate(self, contents: list, gen_config):
        """Call generate_content, retrying transient 503/429 spikes with backoff.

        Gemini flash models intermittently return 503 (high demand) or 429
        (rate/quota) under load; a multi-turn agent makes several calls, so we
        retry rather than abort the whole run on a momentary spike.
        """
        delay = 2.0
        last_exc = None
        for attempt in range(1, 6):
            try:
                return self.client.models.generate_content(
                    model=self.config.gemini_model, contents=contents, config=gen_config
                )
            except Exception as exc:  # SDK raises ServerError/ClientError subclasses
                last_exc = exc
                msg = str(exc)
                transient = any(
                    s in msg
                    for s in ("503", "UNAVAILABLE", "high demand", "overloaded",
                              "429", "RESOURCE_EXHAUSTED")
                )
                if not transient or attempt == 5:
                    raise
                log.warning(
                    "Gemini API busy (attempt %d/5): %s — retrying in %.0fs",
                    attempt, msg.split(".")[0], delay,
                )
                time.sleep(delay)
                delay = min(delay * 2, 20)
        raise last_exc  # pragma: no cover

    def _shot_part(self, name: str):
        _, data = self.tools.take_screenshot(name)
        return self._types.Part.from_bytes(data=data, mime_type="image/png")

    @staticmethod
    def _response_text(response) -> str:
        """Extract text parts safely (avoids the .text property tripping on
        function-call-only responses)."""
        try:
            parts = response.candidates[0].content.parts or []
        except (AttributeError, IndexError, TypeError):
            return ""
        return " ".join(p.text for p in parts if getattr(p, "text", None)).strip()

    def _execute(self, name: str, args: dict) -> str:
        if name == "click_on_screen":
            self.tools.click_on_screen(args["x"], args["y"])
            return f"clicked at ({args['x']}, {args['y']})"
        if name == "double_click":
            self.tools.double_click(args["x"], args["y"])
            return f"double-clicked at ({args['x']}, {args['y']})"
        if name == "send_keys":
            self.tools.send_keys(args["text"])
            return f"typed: {args['text']!r}"
        if name == "scroll":
            self.tools.scroll(args.get("direction", "down"), args.get("amount"))
            return "scrolled"
        if name == "take_screenshot":
            return "screenshot captured"
        return f"unknown tool {name!r}"
