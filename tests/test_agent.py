"""Tests for the agent runner with a mocked LLM."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.agent import AgentReport, AgentRunner, LLMConfig, RepoContext, _parse_steps
from src.manifest import Manifest


def _make_manifest(**overrides) -> Manifest:
    defaults = {
        "allowed_paths": {"read_only": ["**/*.py", "*.md"], "read_write": ["src/**"]},
        "allowed_commands": [r"echo .*"],
        "network": {"allowed_domains": ["api.openai.com"]},
        "agent_task": {"description": "Write a hello world module"},
    }
    defaults.update(overrides)
    return Manifest(**defaults)


def _make_llm_config() -> LLMConfig:
    return LLMConfig(api_key="test-key-123")


# ------------------------------------------------------------------
# _parse_steps
# ------------------------------------------------------------------

class TestParseSteps:
    def test_plain_json(self):
        raw = '[{"action": "read", "path": "foo.py"}]'
        steps = _parse_steps(raw)
        assert len(steps) == 1
        assert steps[0]["action"] == "read"

    def test_json_in_code_fence(self):
        raw = '```json\n[{"action": "write", "path": "x.py", "content": "hi"}]\n```'
        steps = _parse_steps(raw)
        assert len(steps) == 1
        assert steps[0]["action"] == "write"

    def test_invalid_json(self):
        with pytest.raises(ValueError, match="not valid JSON"):
            _parse_steps("this is not json")

    def test_not_a_list(self):
        with pytest.raises(ValueError, match="JSON array"):
            _parse_steps('{"action": "read"}')

    def test_empty_list(self):
        steps = _parse_steps("[]")
        assert steps == []

    def test_malformed_json_with_trailing_comma(self):
        """Trailing commas in JSON are invalid."""
        with pytest.raises(ValueError, match="not valid JSON"):
            _parse_steps('[{"action": "read", "path": "foo.py"},]')

    def test_json_in_triple_backtick_no_lang(self):
        raw = '```\n[{"action": "read", "path": "foo.py"}]\n```'
        steps = _parse_steps(raw)
        assert len(steps) == 1

    def test_nested_code_fence(self):
        """JSON with backticks in content should still parse after stripping outer fence."""
        raw = '```json\n[{"action": "write", "path": "x.py", "content": "```hello```"}]\n```'
        # This won't parse cleanly due to inner backticks, but let's see
        # The stripping logic should handle the outer fence
        with pytest.raises(ValueError):
            _parse_steps(raw)


# ------------------------------------------------------------------
# AgentRunner with mocked LLM
# ------------------------------------------------------------------

class TestAgentRunner:
    def _run_with_plan(
        self,
        tmp_path: Path,
        plan: list[dict],
        summary_text: str = "Mock summary of agent actions.",
    ) -> AgentReport:
        """Create an AgentRunner and run it with a mocked LLM that returns *plan*.

        The first LLM call returns the JSON plan; the second returns a
        human-readable summary string.
        """
        manifest = _make_manifest()
        ctx = RepoContext(root=tmp_path, file_list=["src/main.py"], git_diff_summary="")
        llm_cfg = _make_llm_config()

        runner = AgentRunner(manifest, ctx, llm_cfg)
        # Mock the LLM client: first call = plan, second call = summary
        runner.llm.chat = MagicMock(side_effect=[json.dumps(plan), summary_text])
        return runner.run()

    def test_write_and_report(self, tmp_path: Path):
        plan = [
            {"action": "write", "path": "src/hello.py", "content": "print('hello')"},
        ]
        report = self._run_with_plan(tmp_path, plan)
        assert report.files_modified == ["src/hello.py"]
        assert report.errors == []
        assert (tmp_path / "src" / "hello.py").read_text() == "print('hello')"
        assert "src/hello.py" in report.unified_diff

    def test_read_existing_file(self, tmp_path: Path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "existing.py").write_text("old content")
        plan = [
            {"action": "read", "path": "src/existing.py"},
        ]
        report = self._run_with_plan(tmp_path, plan)
        assert report.files_modified == []
        assert report.errors == []

    def test_run_allowed_command(self, tmp_path: Path):
        plan = [
            {"action": "run", "command": "echo test123"},
        ]
        report = self._run_with_plan(tmp_path, plan)
        assert len(report.commands_executed) == 1
        assert report.commands_executed[0]["exit_code"] == 0
        assert "test123" in report.commands_executed[0]["stdout_excerpt"]
        assert report.errors == []

    def test_denied_write_recorded_as_error(self, tmp_path: Path):
        plan = [
            {"action": "write", "path": "forbidden.env", "content": "SECRET=bad"},
        ]
        report = self._run_with_plan(tmp_path, plan)
        assert report.files_modified == []
        assert len(report.errors) == 1
        assert "Write denied" in report.errors[0]

    def test_denied_command_recorded_as_error(self, tmp_path: Path):
        plan = [
            {"action": "run", "command": "rm -rf /"},
        ]
        report = self._run_with_plan(tmp_path, plan)
        assert len(report.errors) == 1
        assert "not allowed" in report.errors[0]

    def test_unknown_action_recorded_as_error(self, tmp_path: Path):
        plan = [
            {"action": "delete", "path": "something"},
        ]
        report = self._run_with_plan(tmp_path, plan)
        assert len(report.errors) == 1
        assert "Unknown action" in report.errors[0]

    def test_llm_failure_produces_report(self, tmp_path: Path):
        manifest = _make_manifest()
        ctx = RepoContext(root=tmp_path)
        runner = AgentRunner(manifest, ctx, _make_llm_config())
        runner.llm.chat = MagicMock(side_effect=Exception("LLM is down"))
        report = runner.run()
        assert len(report.errors) == 1
        assert "LLM request failed" in report.errors[0]

    def test_diff_shows_new_file(self, tmp_path: Path):
        plan = [
            {"action": "write", "path": "src/new.py", "content": "x = 1\n"},
        ]
        report = self._run_with_plan(tmp_path, plan)
        assert "--- a/src/new.py" in report.unified_diff or "+x = 1" in report.unified_diff

    def test_diff_shows_modification(self, tmp_path: Path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "mod.py").write_text("old = True\n")
        plan = [
            {"action": "write", "path": "src/mod.py", "content": "new = True\n"},
        ]
        report = self._run_with_plan(tmp_path, plan)
        assert "-old = True" in report.unified_diff
        assert "+new = True" in report.unified_diff

    def test_report_json_serializable(self, tmp_path: Path):
        plan = [
            {"action": "write", "path": "src/a.py", "content": "pass"},
            {"action": "run", "command": "echo done"},
        ]
        report = self._run_with_plan(tmp_path, plan)
        data = json.loads(report.to_json())
        assert "files_modified" in data
        assert "commands_executed" in data
        assert "summary" in data

    def test_multi_step_plan(self, tmp_path: Path):
        plan = [
            {"action": "write", "path": "src/a.py", "content": "a = 1"},
            {"action": "write", "path": "src/b.py", "content": "b = 2"},
            {"action": "run", "command": "echo done"},
        ]
        report = self._run_with_plan(tmp_path, plan)
        assert len(report.files_modified) == 2
        assert len(report.commands_executed) == 1
        assert report.errors == []
        # Summary comes from the mock LLM second call
        assert report.summary == "Mock summary of agent actions."

    # -- Additional tests --

    def test_llm_returns_malformed_json(self, tmp_path: Path):
        """When the LLM returns garbage, the agent should report an error."""
        manifest = _make_manifest()
        ctx = RepoContext(root=tmp_path)
        runner = AgentRunner(manifest, ctx, _make_llm_config())
        runner.llm.chat = MagicMock(return_value="this is not json at all {")
        report = runner.run()
        assert len(report.errors) == 1
        assert "not valid JSON" in report.errors[0]
        assert report.files_modified == []

    def test_llm_timeout_produces_report(self, tmp_path: Path):
        """When the LLM times out, the agent should report the failure."""
        import httpx
        manifest = _make_manifest()
        ctx = RepoContext(root=tmp_path)
        runner = AgentRunner(manifest, ctx, _make_llm_config())
        runner.llm.chat = MagicMock(side_effect=httpx.ReadTimeout("timed out"))
        report = runner.run()
        assert len(report.errors) == 1
        assert "LLM request failed" in report.errors[0]

    def test_mixed_success_and_failure_steps(self, tmp_path: Path):
        """Multi-step plan with a mix of successful and failing operations."""
        plan = [
            {"action": "write", "path": "src/good.py", "content": "# good"},
            {"action": "write", "path": "forbidden.env", "content": "BAD"},
            {"action": "run", "command": "echo ok"},
            {"action": "run", "command": "rm -rf /"},
            {"action": "write", "path": "src/also_good.py", "content": "# also good"},
        ]
        report = self._run_with_plan(tmp_path, plan)
        # Two successful writes
        assert "src/good.py" in report.files_modified
        assert "src/also_good.py" in report.files_modified
        # One successful command
        assert len(report.commands_executed) == 1
        # Two errors: forbidden write + denied command
        assert len(report.errors) == 2

    def test_sandbox_violation_during_read(self, tmp_path: Path):
        """Reading a denied file should be recorded as an error, not crash."""
        plan = [
            {"action": "read", "path": "secret.env"},
        ]
        report = self._run_with_plan(tmp_path, plan)
        assert len(report.errors) == 1
        assert "Read denied" in report.errors[0]

    def test_summary_field_in_report(self, tmp_path: Path):
        """The summary field should be populated from the LLM's second call."""
        plan = [{"action": "write", "path": "src/x.py", "content": "x = 1"}]
        custom_summary = "Created x.py with a single variable assignment."
        report = self._run_with_plan(tmp_path, plan, summary_text=custom_summary)
        assert report.summary == custom_summary

    def test_summary_fallback_on_llm_failure(self, tmp_path: Path):
        """If the summary LLM call fails, use the basic summary."""
        manifest = _make_manifest()
        ctx = RepoContext(root=tmp_path)
        runner = AgentRunner(manifest, ctx, _make_llm_config())
        # First call returns a plan; second call (summary) raises
        plan_json = json.dumps([{"action": "write", "path": "src/x.py", "content": "x"}])
        runner.llm.chat = MagicMock(side_effect=[plan_json, Exception("summary failed")])
        report = runner.run()
        # Should fall back to the basic summary
        assert "1 file(s) modified" in report.summary
