"""FastAPI dashboard server.

Endpoints
---------
GET  /              -> the dashboard page
POST /api/run       -> start an agent run (one at a time)
GET  /api/events    -> Server-Sent Events: live log lines + screenshots
GET  /api/health    -> liveness probe (used by container platforms)

Design notes
------------
* The agent uses Playwright's **sync** API, which refuses to run inside an
  asyncio event loop. The server's event loop runs in the main thread, so each
  run is launched in a separate worker thread where the sync API is valid.
* Log lines are captured by attaching a logging handler to the ``agentflow``
  logger; it pushes formatted records onto a thread-safe queue the SSE endpoint
  drains. Screenshots are picked up by polling the screenshot directory for new
  files — so neither the tools nor the agents need to know the web layer exists.
* The browser always runs headless here (a server has no display).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import threading
from queue import Empty, Queue

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from agent.config import Config
from agent.logger import get_logger, setup_logging
from agent.tools import BrowserTools

STATIC_DIR = __import__("pathlib").Path(__file__).resolve().parent / "static"
_LOG_FORMAT = logging.Formatter(
    "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s", datefmt="%H:%M:%S"
)


class RunRequest(BaseModel):
    mode: str = "deterministic"  # "deterministic" | "llm"
    name: str | None = None
    description: str | None = None
    submit: bool = False


def _sse(obj: dict) -> str:
    """Format a dict as a Server-Sent Events ``data:`` frame."""
    return f"data: {json.dumps(obj)}\n\n"


class _QueueLogHandler(logging.Handler):
    """Logging handler that funnels records onto a queue for the SSE stream."""

    def __init__(self, queue: Queue) -> None:
        super().__init__()
        self.queue = queue

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.queue.put_nowait(("log", self.format(record)))
        except Exception:
            pass


class RunManager:
    """Owns the single-run lifecycle and the event stream state."""

    def __init__(self) -> None:
        self.config = Config()
        self.config.headless = True  # no display on a server
        self.config.slow_mo = 0
        self.config.screenshot_dir.mkdir(parents=True, exist_ok=True)

        setup_logging(self.config.log_dir)
        handler = _QueueLogHandler(Queue())
        handler.setFormatter(_LOG_FORMAT)
        logging.getLogger("agentflow").addHandler(handler)

        self.queue: Queue = handler.queue
        self._lock = threading.Lock()
        self.running = False
        self.sent_shots: set[str] = set()

    # ── Run lifecycle ────────────────────────────────────────────────────────

    def start(self, req: RunRequest) -> bool:
        with self._lock:
            if self.running:
                return False
            self.running = True

        # Reset stream state and clear screenshots from any previous run.
        self._drain_queue()
        self.sent_shots.clear()
        for png in self.config.screenshot_dir.glob("*.png"):
            try:
                png.unlink()
            except OSError:
                pass

        threading.Thread(target=self._run, args=(req,), daemon=True).start()
        return True

    def _run(self, req: RunRequest) -> None:
        log = get_logger("dashboard")
        log.info("Dashboard run requested (mode=%s)", req.mode)
        cfg = self.config
        url = cfg.target_url
        name = req.name or cfg.name_value
        description = req.description or cfg.description_value
        tools = BrowserTools(cfg)
        try:
            if req.mode == "llm":
                from agent.llm import LLMAgent

                LLMAgent(tools, cfg).run(url, name, description, submit=req.submit)
            else:
                from agent.deterministic import DeterministicAgent

                DeterministicAgent(tools, cfg).run(
                    url, name, description, submit=req.submit
                )
        except Exception as exc:  # surface to the dashboard, don't crash the server
            log.exception("Run failed: %s", exc)
        finally:
            tools.close()
            self.queue.put_nowait(("done", "Run finished."))
            self.running = False

    # ── Streaming ──────────────────────────────────────────────────────────────

    async def event_stream(self):
        yield _sse({"type": "status", "running": self.running})
        ticks = 0
        while True:
            while True:
                try:
                    kind, payload = self.queue.get_nowait()
                except Empty:
                    break
                if kind == "log":
                    yield _sse({"type": "log", "line": payload})
                elif kind == "done":
                    for frame in self._new_screenshots():
                        yield frame
                    yield _sse({"type": "done"})
                    return
            for frame in self._new_screenshots():
                yield frame
            ticks += 1
            if ticks % 15 == 0:
                yield ": keepalive\n\n"
            await asyncio.sleep(0.3)

    def _new_screenshots(self) -> list[str]:
        frames: list[str] = []
        files = sorted(
            self.config.screenshot_dir.glob("*.png"), key=lambda p: p.stat().st_mtime
        )
        for png in files:
            if png.name in self.sent_shots:
                continue
            self.sent_shots.add(png.name)
            try:
                b64 = base64.b64encode(png.read_bytes()).decode("ascii")
            except OSError:
                continue
            frames.append(_sse({"type": "screenshot", "name": png.name, "b64": b64}))
        return frames

    def _drain_queue(self) -> None:
        while True:
            try:
                self.queue.get_nowait()
            except Empty:
                break


# ── App ──────────────────────────────────────────────────────────────────────

app = FastAPI(title="AgentFlow Dashboard")
manager = RunManager()
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "running": manager.running}


@app.post("/api/run")
def api_run(req: RunRequest):
    if req.mode == "llm" and not manager.config.anthropic_api_key:
        return JSONResponse(
            {"error": "LLM mode needs ANTHROPIC_API_KEY (set it as a Space secret)."},
            status_code=400,
        )
    if not manager.start(req):
        return JSONResponse(
            {"error": "A run is already in progress."}, status_code=409
        )
    return {"status": "started"}


@app.get("/api/events")
async def api_events() -> StreamingResponse:
    return StreamingResponse(manager.event_stream(), media_type="text/event-stream")
