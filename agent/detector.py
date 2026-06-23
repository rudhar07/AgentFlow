"""Intelligent form-field detection.

The agent does not hard-code selectors like ``#name``. Instead it reasons about
the page's structure the way a person would:

1. Find the **Description** field first — it is the most distinctive element, a
   ``<textarea>`` (optionally one whose label/placeholder mentions
   "description", "message", etc.).
2. From there, walk up to the enclosing ``<form>`` and take the first text-like
   ``<input>`` inside it as the **Name / Title** field. Anchoring to the same
   form is what stops the agent from grabbing the docs site's search box.
3. Tag the two chosen elements with a ``data-agentflow`` attribute so Playwright
   can re-locate them, and report rich metadata (label, placeholder, name, id)
   about each — useful for logging and debugging.

The detection JS runs in every frame, so it works whether the form is rendered
inline or inside a component-preview ``<iframe>``.
"""

from __future__ import annotations

from typing import Any

from playwright.sync_api import Frame

from .logger import get_logger
from .tools import BrowserTools

log = get_logger("detector")

# JavaScript executed inside each frame. Returns metadata for the chosen
# Name and Description fields (or null), and tags them with data-agentflow
# so the Python side can locate and measure them.
_DETECT_JS = r"""
() => {
  const isVisible = (el) => {
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 4 && r.height > 4 &&
           s.visibility !== 'hidden' && s.display !== 'none' && s.opacity !== '0';
  };

  const meta = (el) => {
    let label = '';
    if (el.id) {
      const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (l) label = l.innerText;
    }
    if (!label) {
      const wrap = el.closest('label');
      if (wrap) label = wrap.innerText;
    }
    return {
      tag: el.tagName.toLowerCase(),
      type: (el.getAttribute('type') || '').toLowerCase(),
      name: el.getAttribute('name') || '',
      id: el.id || '',
      placeholder: el.getAttribute('placeholder') || '',
      ariaLabel: el.getAttribute('aria-label') || '',
      label: (label || '').replace(/\s+/g, ' ').trim().slice(0, 120),
    };
  };

  const blob = (m) =>
    `${m.placeholder} ${m.label} ${m.name} ${m.ariaLabel} ${m.id}`.toLowerCase();

  const textLike = (el) =>
    el.tagName.toLowerCase() === 'input' &&
    ['', 'text', 'search', 'email'].includes((el.getAttribute('type') || '').toLowerCase());

  // 1) Description = a visible <textarea>, preferring one that looks like it.
  const descKeywords = ['description', 'desc', 'message', 'detail', 'about', 'bio', 'comment'];
  const textareas = [...document.querySelectorAll('textarea')].filter(isVisible);
  let descEl =
    textareas.find((t) => descKeywords.some((k) => blob(meta(t)).includes(k))) ||
    textareas[0] ||
    null;

  // 2) Name/Title = first text-like input in the same form as the description.
  let nameEl = null;
  if (descEl) {
    const form = descEl.closest('form');
    const scope = form || document;
    nameEl = [...scope.querySelectorAll('input')].find((el) => isVisible(el) && textLike(el)) || null;
  }
  // Fallback: first text-like input that lives inside any <form> (avoids the
  // standalone documentation search box, which is not in a form).
  if (!nameEl) {
    nameEl = [...document.querySelectorAll('form input')].find((el) => isVisible(el) && textLike(el)) || null;
  }

  // 3) Submit button: walk up from the Description field and take the last
  // visible Submit/Save button in the nearest enclosing group. This works
  // whether or not the fields live inside a real <form> element.
  let submitEl = null;
  const wantsBtn = (b) =>
    isVisible(b) && /submit|save/i.test((b.innerText || b.value || '').trim());
  let node = descEl || nameEl;
  while (node && node !== document.body && !submitEl) {
    const btns = [...node.querySelectorAll('button, input[type="submit"]')].filter(wantsBtn);
    if (btns.length) submitEl = btns[btns.length - 1]; // Submit usually follows Reset
    node = node.parentElement;
  }
  if (!submitEl) {
    submitEl = [...document.querySelectorAll('button, input[type="submit"]')].filter(wantsBtn)[0] || null;
  }

  if (nameEl) nameEl.setAttribute('data-agentflow', 'name');
  if (descEl) descEl.setAttribute('data-agentflow', 'desc');
  if (submitEl) submitEl.setAttribute('data-agentflow', 'submit');

  return {
    name: nameEl ? meta(nameEl) : null,
    desc: descEl ? meta(descEl) : null,
    submit: submitEl ? { text: (submitEl.innerText || submitEl.value || '').trim().slice(0, 40) } : null,
  };
}
"""


class DetectionResult:
    """The outcome of a detection pass: which frame, and field metadata."""

    def __init__(
        self,
        frame: Frame,
        name: dict | None,
        desc: dict | None,
        submit: dict | None = None,
    ) -> None:
        self.frame = frame
        self.name = name
        self.desc = desc
        self.submit = submit

    def __bool__(self) -> bool:
        return self.name is not None or self.desc is not None


class ElementDetector:
    """Locates the Name and Description fields and computes click targets."""

    def __init__(self, tools: BrowserTools) -> None:
        self.tools = tools

    def detect_form_fields(self) -> DetectionResult | None:
        """Run detection across all frames; return the first frame with a hit."""
        page = self.tools.page
        assert page is not None
        for frame in page.frames:
            try:
                result: dict[str, Any] = frame.evaluate(_DETECT_JS)
            except Exception as exc:
                log.debug("Detection failed in frame %s: %s", frame.url, exc)
                continue
            if result and (result.get("name") or result.get("desc")):
                name, desc, submit = (
                    result.get("name"),
                    result.get("desc"),
                    result.get("submit"),
                )
                log.info("Detected Name/Title field: %s", _describe(name))
                log.info("Detected Description field: %s", _describe(desc))
                if submit:
                    log.info("Detected Submit button: %r", submit.get("text"))
                return DetectionResult(frame, name, desc, submit)
        log.warning("No form fields detected on the page.")
        return None

    def click_target(self, frame: Frame, tag: str) -> tuple[float, float]:
        """Return the viewport-space centre (x, y) of a tagged element.

        The element is first scrolled into view; the bounding box returned by
        Playwright is already relative to the main frame, so the coordinates
        feed straight into ``click_on_screen``/``double_click`` — even when the
        element lives inside an iframe.
        """
        locator = frame.locator(f'[data-agentflow="{tag}"]').first
        locator.scroll_into_view_if_needed()
        box = locator.bounding_box()
        if not box:
            raise RuntimeError(f"Could not measure bounding box for {tag!r} field.")
        cx = box["x"] + box["width"] / 2
        cy = box["y"] + box["height"] / 2
        return cx, cy

    def field_top(self, frame: Frame, tag: str) -> float | None:
        """Viewport-relative top offset of a tagged element (for scrolling)."""
        try:
            return frame.evaluate(
                "(t) => { const el = document.querySelector(`[data-agentflow=\"${t}\"]`);"
                " return el ? el.getBoundingClientRect().top : null; }",
                tag,
            )
        except Exception:
            return None

    def field_value(self, frame: Frame, tag: str) -> str:
        """Current value of a tagged input/textarea (used to verify the fill)."""
        try:
            return frame.locator(f'[data-agentflow="{tag}"]').first.input_value()
        except Exception:
            return ""


def _describe(meta: dict | None) -> str:
    if not meta:
        return "<none>"
    parts = [f"<{meta['tag']}"]
    if meta.get("type"):
        parts.append(f" type={meta['type']}")
    parts.append(">")
    descriptor = "".join(parts)
    label = meta.get("label") or meta.get("placeholder") or meta.get("name") or meta.get("id")
    return f"{descriptor} label/placeholder={label!r}"
