"""End-to-end integration tests for Agent Sandbox.

These tests load a manifest, create a sandbox, run a mock agent, and verify
that the output patch and report are correct.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.agent import AgentRunner, LLMConfig, RepoContext
from src.manifest import load_manifest
from src.sandbox import SandboxedFileSystem

from .conftest import make_manifest, make_mock_llm


class TestEndToEnd:
    """Full pipeline: manifest -> sandbox -> agent -> report + patch."""

    def test_full_pipeline_write_and_verify(self, tmp_path: Path):
        """Load manifest, run agent with mocked LLM, verify report and patch."""
        # Create a minimal repo
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "app.py").write_text("# old code\n", encoding="utf-8")

        # Write a manifest file
        manifest_file = tmp_path / ".agent-sandbox.yml"
        manifest_file.write_text(textwrap.dedent("""\
            allowed_paths:
              read_only:
                - "**/*.py"
              read_write:
                - "src/**"
            allowed_commands:
              - "echo .*"
            network:
              allowed_domains:
                - "api.openai.com"
            agent_task:
              description: "Update the app module"
        """), encoding="utf-8")

        manifest = load_manifest(manifest_file)
        ctx = RepoContext(root=tmp_path, file_list=["src/app.py"])
        llm_cfg = LLMConfig(api_key="test-key")

        plan = json.dumps([
            {"action": "read", "path": "src/app.py"},
            {"action": "write", "path": "src/app.py", "content": "# new code\ndef main(): pass\n"},
            {"action": "run", "command": "echo success"},
        ])
        runner = AgentRunner(manifest, ctx, llm_cfg)
        runner.llm.chat = make_mock_llm(plan)

        report = runner.run()

        # Verify report structure
        assert report.files_modified == ["src/app.py"]
        assert report.errors == []
        assert len(report.commands_executed) == 1
        assert report.commands_executed[0]["exit_code"] == 0

        # Verify the file was actually written
        assert (tmp_path / "src" / "app.py").read_text() == "# new code\ndef main(): pass\n"

        # Verify the diff contains expected changes
        assert "-# old code" in report.unified_diff
        assert "+# new code" in report.unified_diff

        # Verify JSON serialization round-trips
        data = json.loads(report.to_json())
        assert data["files_modified"] == ["src/app.py"]
        assert isinstance(data["summary"], str)

    def test_pipeline_with_denied_operations(self, tmp_path: Path):
        """Ensure denied operations are recorded as errors, not raised."""
        manifest = make_manifest()
        ctx = RepoContext(root=tmp_path)
        llm_cfg = LLMConfig(api_key="test-key")

        plan = json.dumps([
            {"action": "write", "path": "secret.env", "content": "BAD=true"},
            {"action": "run", "command": "rm -rf /"},
            {"action": "write", "path": "src/ok.py", "content": "# ok"},
        ])
        runner = AgentRunner(manifest, ctx, llm_cfg)
        runner.llm.chat = make_mock_llm(plan)

        report = runner.run()

        # The .env write and rm command should fail; src/ok.py should succeed
        assert "src/ok.py" in report.files_modified
        assert len(report.errors) == 2
        assert any("Write denied" in e for e in report.errors)
        assert any("not allowed" in e for e in report.errors)

    def test_pipeline_with_empty_plan(self, tmp_path: Path):
        """Agent returns an empty plan -- no files modified, no errors."""
        manifest = make_manifest()
        ctx = RepoContext(root=tmp_path)
        llm_cfg = LLMConfig(api_key="test-key")

        runner = AgentRunner(manifest, ctx, llm_cfg)
        runner.llm.chat = make_mock_llm("[]", "No changes were needed.")

        report = runner.run()
        assert report.files_modified == []
        assert report.errors == []
        assert report.commands_executed == []

    def test_pipeline_manifest_from_file(self, tmp_path: Path):
        """Test the load_manifest -> AgentRunner integration."""
        manifest_file = tmp_path / ".agent-sandbox.yml"
        manifest_file.write_text(textwrap.dedent("""\
            allowed_paths:
              read_write:
                - "output.txt"
            agent_task:
              description: "Create an output file"
        """), encoding="utf-8")

        manifest = load_manifest(manifest_file)
        ctx = RepoContext(root=tmp_path)
        llm_cfg = LLMConfig(api_key="test-key")

        plan = json.dumps([
            {"action": "write", "path": "output.txt", "content": "done\n"},
        ])
        runner = AgentRunner(manifest, ctx, llm_cfg)
        runner.llm.chat = make_mock_llm(plan)

        report = runner.run()
        assert report.files_modified == ["output.txt"]
        assert (tmp_path / "output.txt").read_text() == "done\n"
        assert report.errors == []

    def test_pipeline_with_fetch_and_comment(self, tmp_path: Path, monkeypatch):
        """Test fetch and comment actions in the full pipeline."""
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "app.py").write_text("print('hello')\n", encoding="utf-8")

        manifest_file = tmp_path / ".agent-sandbox.yml"
        manifest_file.write_text(textwrap.dedent("""\
            allowed_paths:
              read_write:
                - "src/**"
            network:
              allowed_domains:
                - "example.com"
            agent_task:
              description: "Fetch and comment"
        """), encoding="utf-8")

        manifest = load_manifest(manifest_file)
        ctx = RepoContext(root=tmp_path)
        llm_cfg = LLMConfig(api_key="test-key")

        plan = json.dumps([
            {"action": "fetch", "url": "https://example.com/api"},
            {"action": "comment", "path": "src/app.py", "line": 1, "text": "Looks good!"},
        ])
        runner = AgentRunner(manifest, ctx, llm_cfg)
        runner.llm.chat = make_mock_llm(plan)

        # Mock httpx.Client.get for the fetch action
        class MockResponse:
            def __init__(self, text, status_code=200):
                self.text = text
                self.status_code = status_code
            def raise_for_status(self):
                pass
        
        import httpx
        def mock_get(self, url, **kwargs):
            return MockResponse('{"data": 123}')
        monkeypatch.setattr(httpx.Client, "get", mock_get)

        report = runner.run()

        assert not report.errors
        assert len(report.fetches) == 1
        assert report.fetches[0]["url"] == "https://example.com/api"
        assert len(report.comments) == 1
        assert report.comments[0]["path"] == "src/app.py"
        assert report.comments[0]["text"] == "Looks good!"

    def test_report_json_contains_all_fields(self, tmp_path: Path):
        """Verify the JSON report has all expected top-level keys."""
        manifest = make_manifest()
        ctx = RepoContext(root=tmp_path)
        llm_cfg = LLMConfig(api_key="test-key")

        plan = json.dumps([{"action": "run", "command": "echo hello"}])
        runner = AgentRunner(manifest, ctx, llm_cfg)
        runner.llm.chat = make_mock_llm(plan)

        report = runner.run()
        data = json.loads(report.to_json())
        for key in ("files_modified", "commands_executed", "fetches", "comments", "errors", "summary", "unified_diff"):
            assert key in data, f"Missing key: {key}"
