"""Web dashboard for AgentFlow (FastAPI + Server-Sent Events).

Serves a single-page UI that triggers an agent run and streams the agent's log
lines and screenshots live as it works. This is the surface that gets deployed
(Docker / Hugging Face Spaces); the agent itself is unchanged.
"""
