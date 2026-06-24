"""The Gemini-vision agent (optional, element-grounded).

A general "AI-driven browser control" brain backed by **Google Gemini** via the
``google-genai`` SDK. It accepts a free-form ``task`` (e.g. "open YouTube and
search for lofi") as well as the default Name/Description form fill.

Why element-grounded (not raw pixel clicking)? Gemini is a strong reasoner but a
weak pixel-pointer — asked for raw (x, y) on a screenshot it misclicks. So each
turn we hand it BOTH a screenshot *and* a numbered list of the interactive
elements currently on screen (built from the DOM). Gemini chooses an element by
**index**; we look up that element's exact bounding-box centre and click it via
``click_on_screen(x, y)``. Gemini reasons; Playwright targets precisely.

Requires a Gemini API key (``GEMINI_API_KEY`` or ``GOOGLE_API_KEY``). The
deterministic agent needs no key and is the reliable choice; this is the
open-ended one.
"""

from __future__ import annotations

import time

from .config import Config
from .logger import get_logger
from .tools import BrowserTools

log = get_logger("gemini")

# JS that tags every visible, in-viewport interactive element with data-af-idx
# and returns its index, tag, text/label, current value, and centre coords.
_SNAPSHOT_JS = r"""
() => {
  const vw = window.innerWidth, vh = window.innerHeight;
  const sel = 'input, textarea, select, button, a[href], [role="button"],'
            + ' [role="link"], [role="textbox"], [contenteditable="true"]';
  document.querySelectorAll('[data-af-idx]').forEach((e) => e.removeAttribute('data-af-idx'));
  const cand = [];
  for (const el of document.querySelectorAll(sel)) {
    const r = el.getBoundingClientRect();
    if (r.width < 3 || r.height < 3) continue;
    const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
    if (cx < 0 || cx > vw) continue;                  // horizontally on-screen
    if (r.bottom < -40 || r.top > vh + 700) continue; // on screen, or just below the fold
    const s = getComputedStyle(el);
    if (s.visibility === 'hidden' || s.display === 'none' || s.opacity === '0') continue;
    if (el.disabled) continue;
    let label = '';
    if (el.id) { const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (l) label = l.innerText; }
    if (!label) { const w = el.closest('label'); if (w) label = w.innerText; }
    // Prefer the human label/aria-label so a field reads as "Bug Title", not
    // its placeholder; fall back to innerText (buttons/links) then placeholder.
    const text = (label || el.getAttribute('aria-label') || el.innerText
                  || el.getAttribute('placeholder') || '')
      .replace(/\s+/g, ' ').trim().slice(0, 70);
    const tag = el.tagName.toLowerCase();
    const role = el.getAttribute('role') || '';
    // Rank form controls (fields, then buttons) ahead of plain links, so the
    // actionable elements get low indices even on link-heavy pages.
    let prio = 3;
    if (tag === 'input' || tag === 'textarea' || tag === 'select'
        || el.isContentEditable || role === 'textbox') prio = 0;
    else if (tag === 'button' || role === 'button') prio = 1;
    else if (tag === 'a' || role === 'link') prio = 2;
    cand.push({
      el, prio, tag,
      type: (el.getAttribute('type') || '').toLowerCase(),
      text: text,
      value: (el.value || '').slice(0, 50),
      x: Math.round(cx), y: Math.round(cy),
    });
  }
  cand.sort((a, b) => a.prio - b.prio); // stable sort keeps DOM order within a group
  const out = [];
  cand.slice(0, 60).forEach((c, i) => {
    c.el.setAttribute('data-af-idx', String(i));
    out.push({ idx: i, tag: c.tag, type: c.type, text: c.text, value: c.value, x: c.x, y: c.y });
  });
  return out;
}
"""

_SYSTEM_FORM = """You are AgentFlow, an autonomous web-automation agent driving a real browser.

Each turn you receive a screenshot AND a numbered list of the interactive elements \
currently on screen. Act by referencing an element's INDEX from the latest list:
- type_text(index, text): focus that field and type the text.
- click_element(index): click that button/link/element.
- scroll(direction): scroll "down"/"up" to reveal more elements, then you'll get an updated list.
- report_done(summary): call when the task is complete.
Only use indices that appear in the most recent list.

YOUR TASK:
1. Find the text field for the Name/Title (it may be labelled "Name", "Title", or \
"Bug Title") and type_text into it: "{name}"
2. Find the multi-line "Description" field and type_text into it: "{description}"
{submit_clause}
The element list shows each field's current contents as value=... after you type. Do \
NOT call report_done until BOTH the title and Description fields show the values you \
typed. Never report done before you have actually called type_text for each field."""

_SYSTEM_TASK = """You are AgentFlow, an autonomous web-automation agent driving a real browser.

Each turn you receive a screenshot AND a numbered list of the interactive elements \
currently on screen. Act by referencing an element's INDEX from the latest list:
- type_text(index, text): focus that field and type the text.
- click_element(index): click that button/link/element.
- scroll(direction): scroll "down"/"up" to reveal more elements, then you'll get an updated list.
- report_done(summary): call when the task is complete.
Only use indices that appear in the most recent list.

YOUR TASK: {task}

Work one action at a time; after each action you receive an updated screenshot and \
element list. Be efficient and deliberate. Do not call report_done until the task is \
actually accomplished and visible in the screenshot/element list — never report done \
before you have performed the necessary actions."""


class GeminiAgent:
    """Element-grounded browser agent driven by Gemini."""

    def __init__(self, tools: BrowserTools, config: Config) -> None:
        self.tools = tools
        self.config = config
        if not config.gemini_api_key:
            raise RuntimeError(
                "Gemini mode requires a key: set GEMINI_API_KEY (or GOOGLE_API_KEY) "
                "in your environment or .env, or use the deterministic agent (the default)."
            )
        from google import genai
        from google.genai import types

        self._types = types
        self.client = genai.Client(api_key=config.gemini_api_key)
        self._frame = None  # frame the current element list belongs to

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
            system = _SYSTEM_TASK.format(task=task)
        else:
            submit_clause = "3. Click the Submit button.\n" if submit else ""
            system = _SYSTEM_FORM.format(
                name=name_value, description=description_value, submit_clause=submit_clause
            )

        gen_config = types.GenerateContentConfig(
            system_instruction=system,
            tools=self._tool_declarations(),
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )

        elements = self._snapshot()
        contents = [
            types.Content(
                role="user",
                parts=[
                    types.Part(text="Begin the task.\n\n" + self._format_elements(elements)),
                    self._shot_part("gemini-00-initial"),
                ],
            )
        ]

        for step in range(1, self.config.max_steps + 1):
            log.info("--- Gemini step %d/%d ---", step, self.config.max_steps)
            response = self._generate(contents, gen_config)

            text = self._response_text(response)
            if text:
                log.info("Gemini: %s", text)

            calls = response.function_calls
            if not calls:
                log.info("Gemini stopped without a tool call; ending loop.")
                break

            contents.append(response.candidates[0].content)

            if any(fc.name == "report_done" for fc in calls):
                summary = next(
                    (dict(fc.args or {}).get("summary", "") for fc in calls if fc.name == "report_done"),
                    "",
                )
                log.info("Gemini reported done: %s", summary)
                break

            # Execute against the element list Gemini just saw (still tagged in the DOM).
            tool_parts = []
            for fc in calls:
                output = self._execute(fc.name, dict(fc.args or {}))
                tool_parts.append(
                    types.Part.from_function_response(name=fc.name, response={"result": output})
                )

            # Re-snapshot + fresh screenshot for the next turn.
            elements = self._snapshot()
            tool_parts.append(types.Part(text=self._format_elements(elements)))
            tool_parts.append(self._shot_part(f"gemini-{step:02d}"))
            contents.append(types.Content(role="tool", parts=tool_parts))
        else:
            log.warning("Reached max steps (%d).", self.config.max_steps)

        self.tools.take_screenshot("gemini-final")
        log.info("=== Gemini-vision agent finished ===")

    # ── Element grounding ────────────────────────────────────────────────────

    def _snapshot(self) -> list[dict]:
        """Tag + list visible interactive elements; pick the richest frame."""
        page = self.tools.page
        best: list[dict] = []
        best_frame = page.main_frame
        for frame in page.frames:
            try:
                els = frame.evaluate(_SNAPSHOT_JS)
            except Exception:
                continue
            if len(els) > len(best):
                best, best_frame = els, frame
        self._frame = best_frame
        return best

    @staticmethod
    def _format_elements(elements: list[dict]) -> str:
        if not elements:
            return "Interactive elements: (none visible — use scroll to reveal more)."
        lines = []
        for e in elements:
            typ = f" type={e['type']}" if e.get("type") else ""
            val = f" value={e['value']!r}" if e.get("value") else ""
            lines.append(f"[{e['idx']}] <{e['tag']}{typ}> {e['text']!r}{val}")
        return "Interactive elements currently on screen (reference by index):\n" + "\n".join(lines)

    def _execute(self, name: str, args: dict) -> str:
        if name == "type_text":
            idx = int(args["index"])
            text = str(args.get("text", ""))
            focused = self._click_index(idx)
            if focused.startswith("no element"):
                return focused
            loc = self._frame.locator(f'[data-af-idx="{idx}"]').first
            try:
                if loc.input_value():
                    self.tools.press_key("ControlOrMeta+A")
            except Exception:
                pass
            self.tools.send_keys(text)
            return f"typed into [{idx}]: {text!r}"
        if name == "click_element":
            return self._click_index(int(args["index"]))
        if name == "scroll":
            self.tools.scroll(args.get("direction", "down"), args.get("amount"))
            return "scrolled"
        return f"unknown tool {name!r}"

    def _click_index(self, idx: int) -> str:
        loc = self._frame.locator(f'[data-af-idx="{idx}"]').first
        if loc.count() == 0:
            return f"no element [{idx}] in the current list"
        try:
            loc.scroll_into_view_if_needed(timeout=5000)
        except Exception:
            pass
        box = loc.bounding_box()
        if not box:
            return f"element [{idx}] is not clickable"
        cx = box["x"] + box["width"] / 2
        cy = box["y"] + box["height"] / 2
        self.tools.click_on_screen(cx, cy)
        return f"clicked [{idx}] at ({cx:.0f}, {cy:.0f})"

    # ── Gemini plumbing ──────────────────────────────────────────────────────

    def _tool_declarations(self) -> list:
        types = self._types
        fd = types.FunctionDeclaration
        idx_prop = {"index": {"type": "integer", "description": "Element index from the list"}}
        return [
            types.Tool(
                function_declarations=[
                    fd(
                        name="type_text",
                        description="Focus the field at the given index and type text into it.",
                        parameters_json_schema={
                            "type": "object",
                            "properties": {**idx_prop, "text": {"type": "string"}},
                            "required": ["index", "text"],
                        },
                    ),
                    fd(
                        name="click_element",
                        description="Click the element (button/link/field) at the given index.",
                        parameters_json_schema={
                            "type": "object",
                            "properties": {**idx_prop},
                            "required": ["index"],
                        },
                    ),
                    fd(
                        name="scroll",
                        description="Scroll the page to reveal more elements.",
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
        """generate_content with retry on transient 503/429 demand spikes."""
        delay = 2.0
        last_exc = None
        for attempt in range(1, 6):
            try:
                return self.client.models.generate_content(
                    model=self.config.gemini_model, contents=contents, config=gen_config
                )
            except Exception as exc:
                last_exc = exc
                msg = str(exc)
                transient = any(
                    s in msg
                    for s in ("503", "UNAVAILABLE", "high demand", "overloaded",
                              "429", "RESOURCE_EXHAUSTED", "500", "INTERNAL",
                              # transient network/DNS blips
                              "getaddrinfo", "Connection", "ConnectError",
                              "Timeout", "timed out", "Temporary failure", "Errno")
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
        try:
            parts = response.candidates[0].content.parts or []
        except (AttributeError, IndexError, TypeError):
            return ""
        return " ".join(p.text for p in parts if getattr(p, "text", None)).strip()
