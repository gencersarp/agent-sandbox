"""Tests for sandbox enforcement: file-system, commands, network."""

import os
import threading
from pathlib import Path

import pytest

from src.manifest import Manifest
from src.sandbox import (
    NetworkGuard,
    SandboxedCommandRunner,
    SandboxedFileSystem,
    SandboxViolationError,
)


def _make_manifest(**overrides) -> Manifest:
    defaults = {
        "allowed_paths": {"read_only": ["**/*.py", "*.txt"], "read_write": ["src/**"]},
        "allowed_commands": [r"echo .*", r"ls.*"],
        "network": {"allowed_domains": ["api.openai.com", "*.example.com"]},
        "agent_task": {"description": "testing"},
    }
    defaults.update(overrides)
    return Manifest(**defaults)


# ------------------------------------------------------------------
# SandboxedFileSystem
# ------------------------------------------------------------------

class TestSandboxedFileSystem:
    def test_read_allowed(self, tmp_path: Path):
        (tmp_path / "hello.py").write_text("print('hi')", encoding="utf-8")
        fs = SandboxedFileSystem(_make_manifest(), tmp_path)
        assert fs.read("hello.py") == "print('hi')"

    def test_read_denied(self, tmp_path: Path):
        (tmp_path / "secret.env").write_text("KEY=VAL", encoding="utf-8")
        fs = SandboxedFileSystem(_make_manifest(), tmp_path)
        with pytest.raises(SandboxViolationError, match="Read denied"):
            fs.read("secret.env")

    def test_write_allowed(self, tmp_path: Path):
        fs = SandboxedFileSystem(_make_manifest(), tmp_path)
        fs.write("src/new.py", "# new file")
        assert (tmp_path / "src" / "new.py").read_text() == "# new file"
        assert "src/new.py" in fs.files_modified

    def test_write_denied(self, tmp_path: Path):
        fs = SandboxedFileSystem(_make_manifest(), tmp_path)
        with pytest.raises(SandboxViolationError, match="Write denied"):
            fs.write("README.md", "nope")

    def test_write_creates_parent_dirs(self, tmp_path: Path):
        fs = SandboxedFileSystem(_make_manifest(), tmp_path)
        fs.write("src/deep/nested/file.py", "ok")
        assert (tmp_path / "src" / "deep" / "nested" / "file.py").is_file()

    def test_read_own_writes(self, tmp_path: Path):
        fs = SandboxedFileSystem(_make_manifest(), tmp_path)
        fs.write("src/foo.py", "content A")
        assert fs.read("src/foo.py") == "content A"

    def test_path_traversal_blocked(self, tmp_path: Path):
        fs = SandboxedFileSystem(_make_manifest(), tmp_path)
        with pytest.raises(SandboxViolationError, match="escapes repo root"):
            fs.read("../../etc/passwd")

    def test_list_files(self, tmp_path: Path):
        (tmp_path / "a.py").write_text("", encoding="utf-8")
        (tmp_path / "b.txt").write_text("", encoding="utf-8")
        (tmp_path / "c.env").write_text("", encoding="utf-8")
        fs = SandboxedFileSystem(_make_manifest(), tmp_path)
        listed = fs.list_files(".")
        assert "a.py" in listed
        assert "b.txt" in listed
        assert "c.env" not in listed  # not in allowed patterns

    def test_list_files_nonexistent_dir(self, tmp_path: Path):
        fs = SandboxedFileSystem(_make_manifest(), tmp_path)
        with pytest.raises(FileNotFoundError):
            fs.list_files("nonexistent")

    def test_read_nonexistent_file(self, tmp_path: Path):
        fs = SandboxedFileSystem(_make_manifest(), tmp_path)
        with pytest.raises(FileNotFoundError):
            fs.read("does_not_exist.py")

    # -- Additional security tests --

    def test_path_traversal_dotdot_etc_passwd(self, tmp_path: Path):
        """Explicit ../../etc/passwd attack."""
        fs = SandboxedFileSystem(_make_manifest(), tmp_path)
        with pytest.raises(SandboxViolationError, match="escapes repo root"):
            fs.read("../../etc/passwd")

    def test_path_traversal_dotdot_write(self, tmp_path: Path):
        """Path traversal via write should also be blocked."""
        fs = SandboxedFileSystem(_make_manifest(), tmp_path)
        with pytest.raises(SandboxViolationError, match="escapes repo root"):
            fs.write("../../../tmp/evil.py", "hacked")

    def test_path_traversal_encoded(self, tmp_path: Path):
        """Path with .. segments deeper in the path."""
        fs = SandboxedFileSystem(_make_manifest(), tmp_path)
        with pytest.raises(SandboxViolationError, match="escapes repo root"):
            fs.read("src/../../../../../../etc/shadow")

    def test_symlink_does_not_escape(self, tmp_path: Path):
        """Symlink pointing outside repo root should be blocked."""
        link = tmp_path / "src" / "sneaky.py"
        link.parent.mkdir(parents=True, exist_ok=True)
        # Create a symlink pointing to /etc/hosts
        try:
            link.symlink_to("/etc/hosts")
        except OSError:
            pytest.skip("Cannot create symlinks on this OS")
        fs = SandboxedFileSystem(_make_manifest(), tmp_path)
        # resolve() follows the symlink, so this should escape repo root
        with pytest.raises(SandboxViolationError, match="escapes repo root"):
            fs.read("src/sneaky.py")

    def test_concurrent_file_access(self, tmp_path: Path):
        """Multiple threads writing to the sandbox concurrently."""
        fs = SandboxedFileSystem(_make_manifest(), tmp_path)
        errors: list[Exception] = []

        def write_file(i: int) -> None:
            try:
                fs.write(f"src/file_{i}.py", f"# file {i}\n")
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=write_file, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        assert len(fs.files_modified) == 10

    def test_large_file_write_and_read(self, tmp_path: Path):
        """Writing and reading a large file should work."""
        fs = SandboxedFileSystem(_make_manifest(), tmp_path)
        large_content = "x" * (1024 * 1024)  # 1 MB
        fs.write("src/large.py", large_content)
        assert fs.read("src/large.py") == large_content

    def test_empty_file_write_and_read(self, tmp_path: Path):
        """Writing and reading an empty file should work."""
        fs = SandboxedFileSystem(_make_manifest(), tmp_path)
        fs.write("src/empty.py", "")
        assert fs.read("src/empty.py") == ""

    def test_glob_doublestar_py(self, tmp_path: Path):
        """**/*.py matches files at any depth."""
        (tmp_path / "top.py").write_text("top", encoding="utf-8")
        sub = tmp_path / "a" / "b" / "c"
        sub.mkdir(parents=True)
        (sub / "deep.py").write_text("deep", encoding="utf-8")
        fs = SandboxedFileSystem(_make_manifest(), tmp_path)
        assert fs.read("top.py") == "top"
        assert fs.read("a/b/c/deep.py") == "deep"

    def test_glob_star_txt(self, tmp_path: Path):
        """*.txt matches only at the root level."""
        (tmp_path / "root.txt").write_text("root", encoding="utf-8")
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "nested.txt").write_text("nested", encoding="utf-8")
        fs = SandboxedFileSystem(_make_manifest(), tmp_path)
        assert fs.read("root.txt") == "root"
        # *.txt should NOT match sub/nested.txt
        with pytest.raises(SandboxViolationError):
            fs.read("sub/nested.txt")

    def test_glob_src_doublestar(self, tmp_path: Path):
        """src/** matches any file under src/."""
        src = tmp_path / "src"
        deep = src / "a" / "b"
        deep.mkdir(parents=True)
        (src / "top.py").write_text("top", encoding="utf-8")
        (deep / "deep.txt").write_text("deep", encoding="utf-8")
        fs = SandboxedFileSystem(_make_manifest(), tmp_path)
        # Both should be writable (src/** is in read_write)
        assert fs.read("src/top.py") == "top"
        assert fs.read("src/a/b/deep.txt") == "deep"


# ------------------------------------------------------------------
# SandboxedCommandRunner
# ------------------------------------------------------------------

class TestSandboxedCommandRunner:
    def test_allowed_command(self, tmp_path: Path):
        runner = SandboxedCommandRunner(_make_manifest(), cwd=tmp_path)
        result = runner.run("echo hello world")
        assert result.ok
        assert "hello world" in result.stdout

    def test_denied_command(self, tmp_path: Path):
        runner = SandboxedCommandRunner(_make_manifest(), cwd=tmp_path)
        with pytest.raises(SandboxViolationError, match="not allowed"):
            runner.run("rm -rf /")

    def test_command_exit_code(self, tmp_path: Path):
        runner = SandboxedCommandRunner(_make_manifest(), cwd=tmp_path)
        result = runner.run("echo fail && exit 1")
        assert isinstance(result.exit_code, int)

    def test_history_recorded(self, tmp_path: Path):
        runner = SandboxedCommandRunner(_make_manifest(), cwd=tmp_path)
        runner.run("echo one")
        runner.run("echo two")
        assert len(runner.history) == 2
        assert runner.history[0].command == "echo one"

    def test_timeout(self, tmp_path: Path):
        manifest = _make_manifest(allowed_commands=[r"sleep .*"])
        runner = SandboxedCommandRunner(manifest, cwd=tmp_path, timeout=1)
        result = runner.run("sleep 10", timeout=1)
        assert result.exit_code == -1
        assert "timed out" in result.stderr

    def test_ls_command(self, tmp_path: Path):
        (tmp_path / "file.txt").write_text("x")
        runner = SandboxedCommandRunner(_make_manifest(), cwd=tmp_path)
        result = runner.run("ls")
        assert result.ok
        assert "file.txt" in result.stdout


# ------------------------------------------------------------------
# NetworkGuard
# ------------------------------------------------------------------

class TestNetworkGuard:
    def test_exact_domain_allowed(self):
        guard = NetworkGuard(_make_manifest())
        guard.check_url("https://api.openai.com/v1/chat/completions")  # should not raise

    def test_wildcard_domain_allowed(self):
        guard = NetworkGuard(_make_manifest())
        guard.check_url("https://sub.example.com/api")  # matches *.example.com

    def test_domain_denied(self):
        guard = NetworkGuard(_make_manifest())
        with pytest.raises(SandboxViolationError, match="denied"):
            guard.check_url("https://evil.com/steal")

    def test_no_domains_allowed(self):
        manifest = _make_manifest(network={"allowed_domains": []})
        guard = NetworkGuard(manifest)
        with pytest.raises(SandboxViolationError, match="no domains allowed"):
            guard.check_url("https://anything.com")

    def test_wildcard_matches_bare_domain(self):
        guard = NetworkGuard(_make_manifest())
        guard.check_url("https://example.com/path")  # *.example.com should match example.com too
