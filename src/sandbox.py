"""Sandbox enforcement: file-system, command runner, and network guard."""

from __future__ import annotations

import fnmatch
import logging
import os
import re
import subprocess
import urllib.parse
from pathlib import Path, PurePosixPath
from typing import Optional

from .manifest import Manifest

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# Exceptions
# ------------------------------------------------------------------

class SandboxViolationError(Exception):
    """Raised when an operation violates the sandbox policy."""


# ------------------------------------------------------------------
# SandboxedFileSystem
# ------------------------------------------------------------------

class SandboxedFileSystem:
    """Enforce file-system access according to a :class:`Manifest`."""

    def __init__(self, manifest: Manifest, repo_root: str | Path) -> None:
        self.manifest = manifest
        self.repo_root = Path(repo_root).resolve()
        self._files_written: dict[str, str] = {}  # rel_path -> content

    # -- internal helpers --------------------------------------------------

    def _resolve(self, path: str | Path) -> tuple[Path, str]:
        """Return (absolute_path, repo_relative_path).

        Raises ``SandboxViolationError`` if the resolved path escapes the
        repository root (e.g. via ``..``).
        """
        abs_path = (self.repo_root / path).resolve()
        try:
            rel = abs_path.relative_to(self.repo_root)
        except ValueError:
            raise SandboxViolationError(
                f"Path escapes repo root: {path!r} resolves to {abs_path}"
            )
        return abs_path, str(rel)

    @staticmethod
    def _glob_to_regex(pat: str) -> re.Pattern[str]:
        """Convert a glob pattern with ``**`` support into a compiled regex.

        Handles:
        - ``**`` matches zero or more path segments (including none).
        - ``*`` matches anything within a single path segment (no slashes).
        - ``?`` matches a single character (not a slash).
        """
        i, n = 0, len(pat)
        parts: list[str] = []
        while i < n:
            c = pat[i]
            if c == '*':
                if i + 1 < n and pat[i + 1] == '*':
                    # ** — match zero or more path segments
                    i += 2
                    # Skip trailing slash after ** if present
                    if i < n and pat[i] == '/':
                        i += 1
                    parts.append('(?:.+/)?')
                else:
                    # Single * — match within one segment
                    i += 1
                    parts.append('[^/]*')
            elif c == '?':
                i += 1
                parts.append('[^/]')
            elif c == '.':
                i += 1
                parts.append(r'\.')
            else:
                i += 1
                parts.append(re.escape(c))
        return re.compile('^' + ''.join(parts) + '$')

    @staticmethod
    def _matches_any(rel_path: str, patterns: list[str]) -> bool:
        """Return True if *rel_path* matches at least one glob pattern.

        Properly handles ``**`` recursive patterns:
        - ``**/*.py`` matches ``hello.py`` and ``src/utils/helper.py``
        - ``src/**/*.py`` matches ``src/main.py`` and ``src/a/b.py``
        - ``src/**`` matches ``src/foo.py`` and ``src/a/b/c.txt``
        - ``*.yml`` matches ``config.yml`` but not ``a/config.yml``
        """
        # Normalize to forward slashes
        rel_path = rel_path.replace('\\', '/')
        for pat in patterns:
            regex = SandboxedFileSystem._glob_to_regex(pat)
            if regex.match(rel_path):
                return True
            # Support bare directory patterns like "src/**" matching files inside
            if pat.endswith('/**') and not pat.endswith('/**/*'):
                extended = pat + '/*'
                if SandboxedFileSystem._glob_to_regex(extended).match(rel_path):
                    return True
        return False

    def _can_read(self, rel_path: str) -> bool:
        return self._matches_any(rel_path, self.manifest.all_readable_globs)

    def _can_write(self, rel_path: str) -> bool:
        return self._matches_any(rel_path, self.manifest.all_writable_globs)

    # -- public API --------------------------------------------------------

    def read(self, path: str | Path) -> str:
        """Read a file.  Raises on policy violation or missing file."""
        abs_path, rel = self._resolve(path)
        if not self._can_read(rel):
            raise SandboxViolationError(
                f"Read denied for {rel!r}. Allowed readable patterns: "
                f"{self.manifest.all_readable_globs}"
            )
        # Check in-memory writes first (agent may read its own edits)
        if rel in self._files_written:
            return self._files_written[rel]
        if not abs_path.is_file():
            raise FileNotFoundError(f"File not found: {abs_path}")
        return abs_path.read_text(encoding="utf-8")

    def write(self, path: str | Path, content: str) -> Path:
        """Write *content* to *path*.  Raises on policy violation."""
        abs_path, rel = self._resolve(path)
        if not self._can_write(rel):
            raise SandboxViolationError(
                f"Write denied for {rel!r}. Allowed writable patterns: "
                f"{self.manifest.all_writable_globs}"
            )
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_text(content, encoding="utf-8")
        self._files_written[rel] = content
        return abs_path

    def list_files(self, path: str | Path = ".") -> list[str]:
        """List files under *path* that fall within the allowed-read policy.

        Returns repo-relative paths.
        """
        abs_path, rel_base = self._resolve(path)
        if not abs_path.is_dir():
            raise FileNotFoundError(f"Directory not found: {abs_path}")

        results: list[str] = []
        for root, _dirs, files in os.walk(abs_path):
            for fname in files:
                full = Path(root) / fname
                try:
                    rel = str(full.relative_to(self.repo_root))
                except ValueError:
                    continue
                if self._can_read(rel):
                    results.append(rel)
        results.sort()
        return results

    @property
    def files_modified(self) -> list[str]:
        """Repo-relative paths of all files written during this session."""
        return sorted(self._files_written)


# ------------------------------------------------------------------
# SandboxedCommandRunner
# ------------------------------------------------------------------

class CommandResult:
    """Result of a sandboxed command execution."""

    __slots__ = ("command", "exit_code", "stdout", "stderr")

    def __init__(self, command: str, exit_code: int, stdout: str, stderr: str) -> None:
        self.command = command
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    def __repr__(self) -> str:
        return (
            f"CommandResult(command={self.command!r}, exit_code={self.exit_code}, "
            f"stdout_len={len(self.stdout)}, stderr_len={len(self.stderr)})"
        )


class SandboxedCommandRunner:
    """Execute shell commands only if they match the manifest's allowlist."""

    def __init__(
        self,
        manifest: Manifest,
        cwd: str | Path | None = None,
        timeout: int = 120,
    ) -> None:
        self.manifest = manifest
        self.cwd = str(cwd) if cwd else None
        self.timeout = timeout
        self._compiled: list[re.Pattern[str]] = [
            re.compile(pat) for pat in manifest.allowed_commands
        ]
        self._history: list[CommandResult] = []

    def _is_allowed(self, command: str) -> bool:
        """Return True if *command* matches at least one allowed pattern."""
        for pat in self._compiled:
            if pat.fullmatch(command) or pat.search(command):
                return True
        return False

    def run(self, command: str, timeout: Optional[int] = None) -> CommandResult:
        """Run *command* in a subprocess.  Raises on policy violation."""
        if not self._is_allowed(command):
            raise SandboxViolationError(
                f"Command not allowed: {command!r}. "
                f"Allowed patterns: {self.manifest.allowed_commands}"
            )
        try:
            proc = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                cwd=self.cwd,
                timeout=timeout or self.timeout,
            )
            result = CommandResult(command, proc.returncode, proc.stdout, proc.stderr)
        except subprocess.TimeoutExpired:
            result = CommandResult(command, -1, "", f"Command timed out after {timeout or self.timeout}s")
        self._history.append(result)
        return result

    @property
    def history(self) -> list[CommandResult]:
        return list(self._history)


# ------------------------------------------------------------------
# NetworkGuard
# ------------------------------------------------------------------

class NetworkGuard:
    """Validate outbound URLs against the manifest's domain allowlist."""

    def __init__(self, manifest: Manifest) -> None:
        self.manifest = manifest
        self._allowed = manifest.network.allowed_domains

    def _domain_matches(self, hostname: str, pattern: str) -> bool:
        """Check if *hostname* matches an allowed domain pattern.

        Supports:
        - Exact match: ``api.openai.com``
        - Wildcard prefix: ``*.openai.com`` matches ``api.openai.com``
        """
        if pattern == hostname:
            return True
        if pattern.startswith("*."):
            suffix = pattern[1:]  # e.g. ".openai.com"
            return hostname.endswith(suffix) or hostname == pattern[2:]
        return False

    def check_url(self, url: str) -> None:
        """Raise ``SandboxViolationError`` if *url*'s domain is not allowed."""
        if not self._allowed:
            raise SandboxViolationError(
                f"Network access denied (no domains allowed): {url}"
            )
        parsed = urllib.parse.urlparse(url)
        hostname = parsed.hostname or ""
        for pattern in self._allowed:
            if self._domain_matches(hostname, pattern):
                return
        raise SandboxViolationError(
            f"Network access denied for domain {hostname!r} (url={url}). "
            f"Allowed domains: {self._allowed}"
        )
