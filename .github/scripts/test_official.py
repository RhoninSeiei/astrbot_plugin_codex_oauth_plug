"""Fail closed on a missing real host, then test the plugin in that same process."""

import importlib.util
import os
from pathlib import Path
import sys
import tomllib

import pytest


def main() -> int:
    root = Path(os.environ["OFFICIAL_ASTRBOT_ROOT"]).resolve()
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["project"]["version"] == "4.29.4"

    # Preload the actual host so test helpers cannot silently install a fake one.
    from astrbot.core.provider.sources import openai_source
    from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
    from astrbot.core.star.context import Context

    assert Path(openai_source.__file__).resolve().is_relative_to(root)
    assert callable(ToolLoopAgentRunner.reset)
    assert callable(Context.tool_loop_agent)
    for name in ("context_budget", "usage_recorder"):
        assert importlib.util.find_spec(f"astrbot.core.provider.{name}") is None
    print(f"Official AstrBot 4.29.4: {openai_source.__file__}", flush=True)
    print("Custom budget and usage modules are absent.", flush=True)
    sys.path.insert(0, str(Path.cwd()))
    return int(pytest.main(["-q", "-p", "no:cacheprovider", "--junitxml=test-results.xml", "tests"]))


if __name__ == "__main__":
    code = main()
    # Match upstream CI: database worker threads must not hold a finished run open.
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)
