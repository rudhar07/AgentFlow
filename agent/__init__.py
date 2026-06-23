"""AgentFlow — an intelligent website automation agent.

A mini browser-automation framework (in the spirit of Browser Use) built on
Playwright. It exposes a small set of composable, coordinate-based browser
tools and drives them with two interchangeable "brains":

* ``DeterministicAgent`` — reliable, rule-based element detection. No API key.
* ``LLMAgent`` — Claude reads screenshots and decides where to click.

See README.md and ARCHITECTURE.md for the full design.
"""

__version__ = "1.0.0"
