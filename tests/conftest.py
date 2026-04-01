"""Shared fixtures for Agent Sandbox tests."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from src.manifest import Manifest


# ------------------------------------------------------------------
# Manifest helpers
# ------------------------------------------------------------------

MINIMAL_MANIFEST_YAML = textwrap.dedent("""\
    agent_task:
      description: "Do something useful"
""")

FULL_MANIFEST_YAML = textwrap.dedent("""\
    allowed_paths:
      read_only:
        - "**/*.py"
        - "*.txt"
      read_write:
        - "src/**"
    allowed_commands:
      - "echo .*"
      - "ls.*"
    network:
      allowed_domains:
        - "api.openai.com"
        - "*.example.com"
    agent_task:
      description: "Fix lint issues"
      instructions: "Use ruff only"
""")


def make_manifest(**overrides: Any) -> Manifest:
    """Create a Manifest with sensible defaults, overriding as needed."""
    defaults: dict[str, Any] = {
        "allowed_paths": {"read_only": ["**/*.py", "*.txt"], "read_write": ["src/**"]},
        "allowed_commands": [r"echo .*", r"ls.*"],
        "network": {"allowed_domains": ["api.openai.com", "*.example.com"]},
        "agent_task": {"description": "testing"},
    }
    defaults.update(overrides)
    return Manifest(**defaults)


@pytest.fixture
def sample_manifest() -> Manifest:
    """Return a Manifest with broad read/write permissions for testing."""
    return make_manifest()


@pytest.fixture
def tmp_manifest(tmp_path: Path):
    """Write YAML text to a temp file and return the path."""

    def _write(content: str) -> Path:
        p = tmp_path / ".agent-sandbox.yml"
        p.write_text(textwrap.dedent(content), encoding="utf-8")
        return p

    return _write


# ------------------------------------------------------------------
# Repo fixtures
# ------------------------------------------------------------------

@pytest.fixture
def repo_with_files(tmp_path: Path) -> Path:
    """Create a fake repo directory with a few files."""
    (tmp_path / "hello.py").write_text("print('hello')\n", encoding="utf-8")
    (tmp_path / "readme.txt").write_text("A readme.\n", encoding="utf-8")
    src = tmp_path / "src"
    src.mkdir()
    (src / "main.py").write_text("def main(): pass\n", encoding="utf-8")
    (src / "utils").mkdir()
    (src / "utils" / "helper.py").write_text("def help(): pass\n", encoding="utf-8")
    (tmp_path / "config.yml").write_text("key: value\n", encoding="utf-8")
    (tmp_path / "secret.env").write_text("TOKEN=abc\n", encoding="utf-8")
    return tmp_path


# ------------------------------------------------------------------
# Mock LLM responses
# ------------------------------------------------------------------

MOCK_PLAN_WRITE = json.dumps([
    {"action": "write", "path": "src/hello.py", "content": "print('hello world')"},
])

MOCK_PLAN_READ_WRITE_RUN = json.dumps([
    {"action": "read", "path": "src/main.py"},
    {"action": "write", "path": "src/main.py", "content": "# updated\ndef main(): pass\n"},
    {"action": "run", "command": "echo done"},
])

MOCK_PLAN_DENIED = json.dumps([
    {"action": "write", "path": "forbidden.env", "content": "SECRET=bad"},
])

MOCK_SUMMARY = "The agent modified 1 file and ran 1 command successfully."

MOCK_MALFORMED_JSON = "this is not json at all {"
MOCK_NOT_A_LIST = '{"action": "read", "path": "foo.py"}'


def make_mock_llm(plan: str, summary: str = MOCK_SUMMARY) -> MagicMock:
    """Create a mock LLM chat function that returns *plan* then *summary*."""
    return MagicMock(side_effect=[plan, summary])
