"""Launch the AgentFlow web dashboard.

    python dashboard.py            # serves on http://localhost:7860

Port 7860 is the Hugging Face Spaces default, so the same command works locally
and in the deployed container. Override with the PORT environment variable.
"""

from __future__ import annotations

import os

import uvicorn

if __name__ == "__main__":
    port = int(os.getenv("PORT", "7860"))
    uvicorn.run("web.server:app", host="0.0.0.0", port=port, reload=False)
