"""Tests for new features: ignore patterns, list_dir, git action, and filtered file list."""

from pathlib import Path
import pytest
from src.manifest import Manifest
from src.sandbox import SandboxedFileSystem, SandboxViolationError
from src.agent import AgentRunner, LLMConfig, RepoContext

def _make_manifest(**overrides) -> Manifest:
    defaults = {
        "allowed_paths": {"read_only": ["**/*.py", "*.txt"], "read_write": ["src/**"]},
        "allowed_commands": [r"echo .*", r"git .*"],
        "network": {"allowed_domains": ["api.openai.com"]},
        "agent_task": {"description": "testing"},
        "meta": {"ignore": ["**/node_modules/**", ".git/**", "secret.txt"]}
    }
    # Handle nested meta updates
    if "meta" in overrides:
        meta_defaults = defaults["meta"].copy()
        meta_defaults.update(overrides.pop("meta"))
        defaults["meta"] = meta_defaults
        
    defaults.update(overrides)
    return Manifest(**defaults)

class TestIgnorePatterns:
    def test_read_ignored_file_denied(self, tmp_path: Path):
        (tmp_path / "secret.txt").write_text("shhh", encoding="utf-8")
        fs = SandboxedFileSystem(_make_manifest(), tmp_path)
        with pytest.raises(SandboxViolationError, match="Ignored"):
            fs.read("secret.txt")

    def test_write_ignored_file_denied(self, tmp_path: Path):
        fs = SandboxedFileSystem(_make_manifest(), tmp_path)
        # Even if it matches read_write, ignore should win
        with pytest.raises(SandboxViolationError, match="Ignored"):
            fs.write("src/node_modules/foo.py", "content")

    def test_list_files_excludes_ignored(self, tmp_path: Path):
        (tmp_path / "a.py").write_text("", encoding="utf-8")
        (tmp_path / "secret.txt").write_text("", encoding="utf-8")
        nm = tmp_path / "node_modules"
        nm.mkdir()
        (nm / "index.js").write_text("", encoding="utf-8")
        
        fs = SandboxedFileSystem(_make_manifest(), tmp_path)
        listed = fs.list_files(".")
        assert "a.py" in listed
        assert "secret.txt" not in listed
        assert "node_modules/index.js" not in listed

class TestAgentNewActions:
    @pytest.fixture
    def runner(self, tmp_path: Path):
        manifest = _make_manifest()
        ctx = RepoContext(root=tmp_path, file_list=["src/main.py", "secret.txt", "node_modules/foo.js"])
        cfg = LLMConfig(api_key="fake")
        return AgentRunner(manifest, ctx, cfg)

    def test_list_dir_executes(self, runner, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("print(1)")
        # Just check it doesn't crash and logs correctly
        runner._exec_step({"action": "list_dir", "path": "src"})
        assert not runner.errors

    def test_git_status_executes(self, runner, tmp_path):
        # Initialize a real git repo for the test
        import subprocess
        subprocess.run(["git", "init"], cwd=tmp_path, check=True)
        
        runner._exec_step({"action": "git", "subcommand": "status"})
        assert not runner.errors
        # history[0] because it's the first command
        assert runner.cmd.history[0].command == "git status"

    def test_git_denied_subcommand(self, runner, tmp_path):
        # If the manifest doesn't allow the resulting command
        manifest = _make_manifest(allowed_commands=[r"git status"])
        runner.manifest = manifest
        runner.cmd = runner.cmd.__class__(manifest, cwd=tmp_path) # recreate runner with new manifest
        
        runner._exec_step({"action": "git", "subcommand": "push"})
        assert any("not allowed" in err for err in runner.errors)

class TestFilteredFileList:
    def test_build_user_prompt_filters_files(self, tmp_path: Path):
        from src.agent import _build_user_prompt
        manifest = _make_manifest(allowed_paths={"read_only": ["*.py"], "read_write": []})
        ctx = RepoContext(
            root=tmp_path, 
            file_list=["main.py", "secret.txt", "node_modules/foo.py", "README.md"]
        )
        # node_modules/foo.py is ignored by default in _make_manifest
        # secret.txt is ignored
        # README.md is not in allowed_paths
        
        prompt = _build_user_prompt(manifest, ctx)
        assert "main.py" in prompt
        # Use more specific check because secret.txt might appear in the ignore section
        sections = prompt.split("## ")
        file_list_sec = next(s for s in sections if s.startswith("Repo file list (filtered)"))
        assert "main.py" in file_list_sec
        assert "secret.txt" not in file_list_sec
        assert "node_modules/foo.py" not in file_list_sec
        assert "README.md" not in file_list_sec
