"""AgentFlow CLI entry point.

Examples
--------
Deterministic agent (default, no API key needed), visible browser::

    python main.py

Run headless and submit the form::

    python main.py --headless --submit

LLM-vision agent (needs ANTHROPIC_API_KEY)::

    python main.py --mode llm

Override the values typed into the fields::

    python main.py --name "Jane Doe" --description "A twenty-plus character note."
"""

from __future__ import annotations

import argparse
import sys

from agent.config import Config
from agent.logger import get_logger, setup_logging
from agent.tools import BrowserTools


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agentflow",
        description="An intelligent website automation agent (Playwright + optional Claude).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--mode",
        choices=["deterministic", "llm", "gemini"],
        default="deterministic",
        help="Which agent brain to use: deterministic (rule-based, no key), "
        "llm (Claude vision), or gemini (Google Gemini vision).",
    )
    parser.add_argument("--url", help="Target URL (defaults to the shadcn forms page).")
    parser.add_argument("--name", help="Value to type into the Name/Title field.")
    parser.add_argument("--description", help="Value to type into the Description field.")
    parser.add_argument(
        "--task",
        help="Free-form goal for the gemini agent (e.g. \"open youtube.com and "
        "search for lofi beats\"). Overrides the default form-fill task.",
    )
    parser.add_argument(
        "--headless", action="store_true", help="Run the browser without a visible window."
    )
    parser.add_argument(
        "--submit", action="store_true", help="Click the form's submit button after filling."
    )
    parser.add_argument(
        "--keep-open",
        action="store_true",
        help="Pause before closing the browser so you can inspect the result.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # Build config, then apply CLI overrides.
    config = Config()
    if args.headless:
        config.headless = True
    url = args.url or config.target_url
    name_value = args.name or config.name_value
    description_value = args.description or config.description_value

    setup_logging(config.log_dir)
    log = get_logger("main")
    log.info("AgentFlow starting | mode=%s | %s", args.mode, config)
    log.info("Target: %s", url)

    tools = BrowserTools(config)
    exit_code = 0
    try:
        if args.mode == "deterministic":
            from agent.deterministic import DeterministicAgent

            agent = DeterministicAgent(tools, config)
            result = agent.run(url, name_value, description_value, submit=args.submit)
            if not result.success:
                log.error("Agent did not fill any field: %s", result.message)
                exit_code = 1
        elif args.mode == "llm":
            from agent.llm import LLMAgent

            LLMAgent(tools, config).run(
                url, name_value, description_value, submit=args.submit
            )
        else:  # gemini
            from agent.gemini import GeminiAgent

            GeminiAgent(tools, config).run(
                url, name_value, description_value, submit=args.submit, task=args.task
            )

        if args.keep_open:
            try:
                input("\nPress Enter to close the browser...")
            except EOFError:
                pass
    except KeyboardInterrupt:
        log.warning("Interrupted by user.")
        exit_code = 130
    except Exception as exc:
        log.exception("Run failed: %s", exc)
        exit_code = 1
    finally:
        tools.close()

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
