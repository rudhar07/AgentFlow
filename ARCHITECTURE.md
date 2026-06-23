# AgentFlow — Architecture

This document explains the design decisions behind AgentFlow and walks through
the agent's workflow.

---

## 1. Design goals

The architecture is shaped around four goals:

1. **Functionality** — reliably complete the target task end-to-end.
2. **Code quality** — small, single-responsibility modules.
3. **Agent intelligence** — adapt to the page instead of hard-coding.
4. **Error handling** — fail loudly and safely, never silently.

The key tension is between **reliability** (a demo that always works) and
**intelligence** (genuinely adaptive behaviour). AgentFlow resolves it by separating
*what the agent can do* (a fixed tool belt) from *how it decides* (two
swappable brains), so we get a rock-solid default path **and** a genuinely
AI-driven one.

---

## 2. Layered architecture

```
                ┌──────────────────────────────────────────┐
                │                main.py (CLI)               │
                │   parses flags, wires config + logging     │
                └───────────────────┬────────────────────────┘
                                    │ chooses a brain
              ┌─────────────────────┴─────────────────────┐
              ▼                                            ▼
   ┌────────────────────────┐                ┌──────────────────────────┐
   │  DeterministicAgent     │                │        LLMAgent          │
   │  (rule-based brain)     │                │  (Claude vision brain)   │
   │  deterministic.py       │                │  llm.py                  │
   └───────────┬─────────────┘                └────────────┬─────────────┘
               │ uses                                       │ uses
               ▼                                            │
   ┌────────────────────────┐                              │
   │   ElementDetector       │                              │
   │   detector.py           │  (structure-based detection) │
   └───────────┬─────────────┘                              │
               │                                            │
               └──────────────────┬─────────────────────────┘
                                  ▼
                       ┌────────────────────────┐
                       │      BrowserTools        │  ← the 7 required tools
                       │      tools.py            │
                       └───────────┬──────────────┘
                                   ▼
                       ┌────────────────────────┐
                       │     Playwright           │
                       └────────────────────────┘

      config.py (settings)  ·  logger.py (auditable trace)  — cross-cutting
```

**Why this shape?** Both brains depend only on `BrowserTools`. The tool belt is
the contract; the brains are strategies. Swapping or adding a brain never
touches the tools, and improving a tool benefits both brains. This is the
"modular tools that can be composed together" the brief asks for.

---

## 3. The tool belt (`tools.py`)

`BrowserTools` is a thin, stateful wrapper over a single Playwright session.
Each required capability is one method, and **every method logs its
invocation** — that log *is* the agent's audit trail.

A deliberate decision: **clicks are coordinate-based** (`page.mouse.click(x,
y)` / `dblclick`), matching the `click_on_screen(x, y)` contract, rather than
Playwright's higher-level `locator.click()`. The brains are responsible for
*producing* those coordinates:

- the deterministic brain computes them from an element's bounding box;
- the LLM brain reads them off a screenshot.

This keeps the tool layer dumb and uniform while letting intelligence live in
the brains.

---

## 4. Intelligent element detection (`detector.py`)

Hard-coding `#name` would be brittle and would score poorly on "agent
intelligence" — and would simply be *wrong* here, because the page's field is
labelled "Bug Title", not "Name". Instead, detection mirrors how a person reads
the form:

1. **Anchor on the most distinctive element.** The Description is a
   `<textarea>` — find visible textareas, preferring one whose
   label/placeholder/name reads like a description.
2. **Find the partner field by structure.** Walk up to the textarea's enclosing
   `<form>`, then take the first text-like `<input>` inside it as the
   Name/Title field. Anchoring to the *same form* is what prevents the agent
   from grabbing the docs site's global **search box** — a trap that a naive
   "first text input on the page" heuristic falls into.
3. **Tag + report.** The chosen elements are tagged with a `data-agentflow`
   attribute (so Playwright can re-locate and measure them) and rich metadata
   (label, placeholder, name, id) is logged.

The detection script runs **in every frame**, so it works whether the form is
rendered inline or inside a component-preview `<iframe>`. Because Playwright's
`bounding_box()` returns coordinates relative to the main frame, the computed
click target is correct even for iframe-nested fields.

---

## 5. The two brains

### Deterministic (`deterministic.py`) — default

A straight-line orchestration of the tools:

```
open_browser → navigate_to_url → take_screenshot("01-initial")
   → detect fields
   → for each field: scroll into view → click/double-click centre → send_keys
   → read the value back to VERIFY the fill
   → take_screenshot("02-filled")  [→ submit → "03-submitted"]
```

It deliberately exercises **all seven tools** (single click for Name,
`double_click` for Description, an explicit `scroll` to bring the form into
view) and **self-verifies** by reading each field's value after typing.

### LLM-vision (`llm.py`) — optional

The "computer use" brain. Claude is given a screenshot plus tools named exactly
like the contract (`click_on_screen`, `double_click`, `send_keys`, `scroll`,
`take_screenshot`, `report_done`) and runs a perceive→decide→act loop:

```
send screenshot + goal
loop (bounded by AGENTFLOW_MAX_STEPS):
    Claude returns a tool_use  →  execute it  →  capture a fresh screenshot
    return the screenshot as the tool_result  →  Claude decides the next action
until Claude calls report_done
```

Design choices worth noting:

- **`disable_parallel_tool_use`** forces one action per turn, so each decision
  is made against the freshest screenshot.
- **Adaptive thinking** is enabled; the full assistant turn (thinking blocks
  included) is preserved verbatim across turns, as the API requires.
- The **1280×800 viewport** keeps every screenshot under ~1.15 MP, so Claude
  sees images at native resolution and its (x, y) coordinates map 1:1 to real
  page pixels — no scaling math.
- Defaults to `claude-opus-4-8`; override with `ANTHROPIC_MODEL`.

---

## 6. Cross-cutting concerns

- **Configuration (`config.py`).** One typed `Config` object, populated from env
  vars / `.env`, with sane defaults. Nothing else reads `os.environ`.
- **Logging (`logger.py`).** Console + file handlers under one `agentflow`
  namespace. Tool calls log at INFO; transient issues at DEBUG.
- **Error handling.** Tools guard against use-before-open (`BrowserError`);
  navigation tolerates slow networks; detection failure is reported and
  screenshotted rather than throwing; `main.py` wraps everything and *always*
  closes the browser in a `finally`.

---

## 7. Trade-offs and what I'd add next

- **Why a deterministic default?** A live demo must not depend on network
  latency to an LLM or on the model clicking the right pixel. The deterministic
  brain is fast and repeatable; the LLM brain showcases autonomy.
- **Coordinate clicking vs. selectors.** Coordinates honour the `click_on_screen(x, y)`
  contract and make the LLM and deterministic paths symmetric, at the cost of
  needing a visible, scrolled-into-view element. The detector handles that.
- **Next steps:** a retry/back-off wrapper around flaky actions, an
  accessibility-tree fallback for detection, and a small pytest suite that runs
  the deterministic agent against a local fixture page in CI.
