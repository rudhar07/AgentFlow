"""Central configuration for AgentFlow.

All settings are read from environment variables (optionally via a local
``.env`` file) and exposed through a single :class:`Config` object so the rest
of the codebase never touches ``os.environ`` directly. Every value has a
default, so the agent runs out of the box with zero configuration.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# Load a local .env file if present. Values already set in the real environment
# take precedence over the file (override=False).
load_dotenv(override=False)

# Project root = the directory that contains this package.
_ROOT = Path(__file__).resolve().parent.parent


def _as_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


class Config:
    """Resolved, typed configuration. Construct once and pass around."""

    def __init__(self) -> None:
        # --- Target task -----------------------------------------------------
        self.target_url: str = os.getenv(
            "AGENTFLOW_TARGET_URL",
            "https://ui.shadcn.com/docs/forms/react-hook-form",
        )
        # The page's featured form currently labels its text field "Bug Title"
        # rather than "Name"; the agent locates fields by role, not by a fixed
        # label, so these values simply fill whatever Name/Title + Description
        # fields it finds. Defaults satisfy the demo form's validation
        # (title 5-32 chars, description 20-100 chars).
        self.name_value: str = os.getenv("AGENTFLOW_NAME_VALUE", "AgentFlow Bot")
        self.description_value: str = os.getenv(
            "AGENTFLOW_DESCRIPTION_VALUE",
            "Filled automatically by the AgentFlow website automation agent.",
        )

        # --- Browser ---------------------------------------------------------
        self.browser: str = os.getenv("AGENTFLOW_BROWSER", "chromium")
        self.headless: bool = _as_bool(os.getenv("AGENTFLOW_HEADLESS", "false"))
        self.slow_mo: int = int(os.getenv("AGENTFLOW_SLOW_MO", "150"))
        self.viewport_width: int = int(os.getenv("AGENTFLOW_VIEWPORT_WIDTH", "1280"))
        self.viewport_height: int = int(os.getenv("AGENTFLOW_VIEWPORT_HEIGHT", "800"))
        self.timeout_ms: int = int(os.getenv("AGENTFLOW_TIMEOUT_MS", "30000"))
        self.type_delay_ms: int = int(os.getenv("AGENTFLOW_TYPE_DELAY_MS", "30"))

        # --- LLM-vision agent ------------------------------------------------
        self.anthropic_api_key: str | None = os.getenv("ANTHROPIC_API_KEY") or None
        self.model: str = os.getenv("ANTHROPIC_MODEL", "claude-opus-4-8")
        self.max_steps: int = int(os.getenv("AGENTFLOW_MAX_STEPS", "20"))

        # --- Output paths ----------------------------------------------------
        self.screenshot_dir: Path = Path(
            os.getenv("AGENTFLOW_SCREENSHOT_DIR", str(_ROOT / "screenshots"))
        )
        self.log_dir: Path = Path(os.getenv("AGENTFLOW_LOG_DIR", str(_ROOT / "logs")))

    def __repr__(self) -> str:  # helpful in logs
        return (
            f"Config(browser={self.browser!r}, headless={self.headless}, "
            f"viewport={self.viewport_width}x{self.viewport_height}, "
            f"model={self.model!r})"
        )
