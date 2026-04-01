"""Agent runner --- plans and executes edits via an LLM, sandboxed."""

from __future__ import annotations

import difflib
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import httpx

from .manifest import Manifest
from .sandbox import (
    CommandResult,
    NetworkGuard,
    SandboxedCommandRunner,
    SandboxedFileSystem,
    SandboxViolationError,
)

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# Constants
# ------------------------------------------------------------------

_MAX_RETRIES = 3
_RETRY_DELAYS = [1, 2, 4]  # seconds: exponential backoff

# Transient HTTP status codes that warrant a retry.
_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


# ------------------------------------------------------------------
# Data types
# ------------------------------------------------------------------

@dataclass
class RepoContext:
    """Contextual information about the repo the agent operates on."""

    root: Path
    file_list: list[str] = field(default_factory=list)
    git_diff_summary: str = ""


@dataclass
class LLMConfig:
    """Configuration for the OpenAI-compatible LLM endpoint."""

    api_url: str = "https://api.openai.com/v1/chat/completions"
    api_key: str = ""
    model: str = "gpt-4o"
    temperature: float = 0.2
    max_tokens: int = 4096


@dataclass
class AgentReport:
    """Final structured report produced after a run."""

    files_modified: list[str]
    commands_executed: list[dict[str, Any]]
    errors: list[str]
    summary: str
    unified_diff: str

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(
            {
                "files_modified": self.files_modified,
                "commands_executed": self.commands_executed,
                "errors": self.errors,
                "summary": self.summary,
                "unified_diff": self.unified_diff,
            },
            indent=indent,
        )


# ------------------------------------------------------------------
# LLM client (thin wrapper, OpenAI-compatible)
# ------------------------------------------------------------------

class LLMClient:
    """Minimal OpenAI-compatible chat-completions client with retry logic."""

    def __init__(self, config: LLMConfig, network_guard: Optional[NetworkGuard] = None) -> None:
        self.config = config
        self.network_guard = network_guard

    def _do_request(self, messages: list[dict[str, str]]) -> str:
        """Perform a single HTTP request to the LLM API."""
        if self.network_guard:
            self.network_guard.check_url(self.config.api_url)

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.config.api_key}",
        }
        payload = {
            "model": self.config.model,
            "messages": messages,
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
        }

        with httpx.Client(timeout=120) as client:
            resp = client.post(self.config.api_url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()

        return data["choices"][0]["message"]["content"]

    def chat(self, messages: list[dict[str, str]]) -> str:
        """Send *messages* and return the assistant's text reply.

        Retries up to ``_MAX_RETRIES`` times on transient failures with
        exponential backoff (1 s, 2 s, 4 s).
        """
        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES):
            try:
                return self._do_request(messages)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in _RETRYABLE_STATUS_CODES:
                    last_exc = exc
                    delay = _RETRY_DELAYS[attempt] if attempt < len(_RETRY_DELAYS) else _RETRY_DELAYS[-1]
                    logger.warning(
                        "LLM request failed (HTTP %d), retrying in %ds (attempt %d/%d)...",
                        exc.response.status_code, delay, attempt + 1, _MAX_RETRIES,
                    )
                    time.sleep(delay)
                    continue
                raise
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout) as exc:
                last_exc = exc
                delay = _RETRY_DELAYS[attempt] if attempt < len(_RETRY_DELAYS) else _RETRY_DELAYS[-1]
                logger.warning(
                    "LLM request failed (%s), retrying in %ds (attempt %d/%d)...",
                    type(exc).__name__, delay, attempt + 1, _MAX_RETRIES,
                )
                time.sleep(delay)
                continue

        # All retries exhausted
        raise last_exc  # type: ignore[misc]


# ------------------------------------------------------------------
# Instruction / prompt building
# ------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are a sandboxed coding agent.  You operate inside a CI environment with
strict file-system and command restrictions.

RULES:
- Only read/write files that match the allowed glob patterns.
- Only run commands that match the allowed command patterns.
- Do NOT attempt to escape the sandbox, access files outside allowed patterns,
  run disallowed commands, or contact domains not in the allowlist.  Any such
  attempt will be blocked and logged as a security violation.
- Produce your plan as a JSON array of steps.
- Do NOT include any text, markdown fences, or commentary outside the JSON array.

Each step is a JSON object with an "action" key.  Valid actions:

1. Read a file:
   {"action": "read", "path": "src/main.py"}

2. Write a file (full content):
   {"action": "write", "path": "src/main.py", "content": "import os\\n\\ndef main():\\n    pass\\n"}

3. Run a shell command:
   {"action": "run", "command": "echo hello"}

EXAMPLE — a complete valid response:
[
  {"action": "read", "path": "src/app.py"},
  {"action": "write", "path": "src/app.py", "content": "# updated\\nimport sys\\n"},
  {"action": "run", "command": "echo done"}
]

Respond ONLY with a JSON array of step objects.  No markdown fences, no commentary.
"""

_SUMMARY_PROMPT = """\
You just executed a coding task inside a sandboxed CI environment.
Below is the execution report.  Summarize what was done in 2-3 concise sentences
for a human reviewer.  Focus on what files were changed, what commands were run,
and whether any errors occurred.  Do NOT include JSON or code in your summary.

Report:
{report_json}
"""


def _build_user_prompt(manifest: Manifest, ctx: RepoContext) -> str:
    parts = [
        f"## Task\n{manifest.agent_task.description}",
    ]
    if manifest.agent_task.instructions:
        parts.append(f"## Extra instructions\n{manifest.agent_task.instructions}")
    parts.append(f"## Allowed readable globs\n{manifest.all_readable_globs}")
    parts.append(f"## Allowed writable globs\n{manifest.all_writable_globs}")
    parts.append(f"## Allowed commands\n{manifest.allowed_commands}")
    if ctx.file_list:
        truncated = ctx.file_list[:200]
        parts.append("## Repo file list (truncated to 200)\n" + "\n".join(truncated))
    if ctx.git_diff_summary:
        parts.append(f"## Git diff summary\n{ctx.git_diff_summary}")
    return "\n\n".join(parts)


# ------------------------------------------------------------------
# Step executor
# ------------------------------------------------------------------

def _parse_steps(raw: str) -> list[dict[str, Any]]:
    """Parse the LLM's JSON response into a list of step dicts.

    Tolerates markdown code fences around the JSON.
    """
    text = raw.strip()
    # Strip optional markdown fences
    if text.startswith("```"):
        first_nl = text.index("\n")
        text = text[first_nl + 1:]
    if text.endswith("```"):
        text = text[:text.rfind("```")]
    text = text.strip()
    try:
        steps = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"LLM response is not valid JSON: {exc}\nResponse:\n{text[:500]}") from exc
    if not isinstance(steps, list):
        raise ValueError(f"Expected a JSON array of steps, got {type(steps).__name__}")
    return steps


# ------------------------------------------------------------------
# AgentRunner
# ------------------------------------------------------------------

class AgentRunner:
    """Orchestrates LLM planning + sandboxed execution."""

    def __init__(
        self,
        manifest: Manifest,
        repo_context: RepoContext,
        llm_config: LLMConfig,
    ) -> None:
        self.manifest = manifest
        self.ctx = repo_context
        self.llm = LLMClient(llm_config, NetworkGuard(manifest))
        self.fs = SandboxedFileSystem(manifest, repo_context.root)
        self.cmd = SandboxedCommandRunner(manifest, cwd=repo_context.root)
        self.errors: list[str] = []
        self._original_contents: dict[str, str | None] = {}

    # ------------------------------------------------------------------
    # Snapshotting for diff
    # ------------------------------------------------------------------

    def _snapshot_original(self, rel_path: str) -> None:
        """Store the original content of a file before it is modified."""
        if rel_path in self._original_contents:
            return
        abs_path = self.ctx.root / rel_path
        if abs_path.is_file():
            try:
                self._original_contents[rel_path] = abs_path.read_text(encoding="utf-8")
            except Exception:
                self._original_contents[rel_path] = None
        else:
            self._original_contents[rel_path] = None

    # ------------------------------------------------------------------
    # Step execution
    # ------------------------------------------------------------------

    def _exec_step(self, step: dict[str, Any]) -> None:
        action = step.get("action")
        if action == "read":
            path = step.get("path", "")
            logger.info("READ  %s", path)
            try:
                content = self.fs.read(path)
                logger.debug("Read %d bytes from %s", len(content), path)
            except (SandboxViolationError, FileNotFoundError) as exc:
                self.errors.append(f"read {path}: {exc}")
        elif action == "write":
            path = step.get("path", "")
            content = step.get("content", "")
            logger.info("WRITE %s (%d bytes)", path, len(content))
            try:
                self._snapshot_original(path)
                self.fs.write(path, content)
            except SandboxViolationError as exc:
                self.errors.append(f"write {path}: {exc}")
        elif action == "run":
            command = step.get("command", "")
            logger.info("RUN   %s", command)
            try:
                result = self.cmd.run(command)
                if not result.ok:
                    self.errors.append(
                        f"command failed (exit {result.exit_code}): {command}\n"
                        f"stderr: {result.stderr[:500]}"
                    )
            except SandboxViolationError as exc:
                self.errors.append(f"run {command}: {exc}")
        else:
            self.errors.append(f"Unknown action: {action!r}")

    # ------------------------------------------------------------------
    # Diff generation
    # ------------------------------------------------------------------

    def _generate_diff(self) -> str:
        """Generate a unified diff of all files modified during this session."""
        diffs: list[str] = []
        for rel_path in self.fs.files_modified:
            old = self._original_contents.get(rel_path)
            new = self.fs._files_written.get(rel_path, "")
            old_lines = (old or "").splitlines(keepends=True)
            new_lines = new.splitlines(keepends=True)
            diff = difflib.unified_diff(
                old_lines,
                new_lines,
                fromfile=f"a/{rel_path}",
                tofile=f"b/{rel_path}",
            )
            diffs.append("".join(diff))
        return "\n".join(diffs)

    # ------------------------------------------------------------------
    # LLM summary generation
    # ------------------------------------------------------------------

    def _generate_llm_summary(self, report_data: dict[str, Any]) -> str:
        """Ask the LLM to produce a human-readable summary of the run."""
        compact_report = {
            "files_modified": report_data["files_modified"],
            "commands_executed": [
                {"command": c["command"], "exit_code": c["exit_code"]}
                for c in report_data["commands_executed"]
            ],
            "errors": report_data["errors"],
        }
        prompt = _SUMMARY_PROMPT.format(report_json=json.dumps(compact_report, indent=2))
        try:
            return self.llm.chat([
                {"role": "system", "content": "You are a concise technical writer."},
                {"role": "user", "content": prompt},
            ]).strip()
        except Exception as exc:
            logger.warning("Failed to generate LLM summary: %s", exc)
            return ""

    # ------------------------------------------------------------------
    # Main entry
    # ------------------------------------------------------------------

    def run(self) -> AgentReport:
        """Execute the full agent loop: plan via LLM, execute, report."""
        # 1. Ask the LLM for a plan
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": _build_user_prompt(self.manifest, self.ctx)},
        ]
        logger.info("Requesting plan from LLM (%s)...", self.llm.config.model)
        try:
            raw_plan = self.llm.chat(messages)
        except Exception as exc:
            return AgentReport(
                files_modified=[],
                commands_executed=[],
                errors=[f"LLM request failed: {exc}"],
                summary="Agent failed to obtain a plan from the LLM.",
                unified_diff="",
            )

        # 2. Parse steps
        try:
            steps = _parse_steps(raw_plan)
        except ValueError as exc:
            return AgentReport(
                files_modified=[],
                commands_executed=[],
                errors=[str(exc)],
                summary="Agent could not parse LLM plan.",
                unified_diff="",
            )

        # 3. Execute each step
        for i, step in enumerate(steps):
            logger.info("Step %d/%d", i + 1, len(steps))
            self._exec_step(step)

        # 4. Build report data
        cmd_history = [
            {
                "command": r.command,
                "exit_code": r.exit_code,
                "stdout_excerpt": r.stdout[:500],
                "stderr_excerpt": r.stderr[:500],
            }
            for r in self.cmd.history
        ]

        diff = self._generate_diff()
        n_files = len(self.fs.files_modified)
        n_cmds = len(self.cmd.history)
        n_errs = len(self.errors)
        basic_summary = (
            f"Agent completed. {n_files} file(s) modified, "
            f"{n_cmds} command(s) executed, {n_errs} error(s)."
        )

        # 5. Generate LLM summary
        report_data = {
            "files_modified": self.fs.files_modified,
            "commands_executed": cmd_history,
            "errors": self.errors,
        }
        llm_summary = self._generate_llm_summary(report_data)
        summary = llm_summary if llm_summary else basic_summary

        return AgentReport(
            files_modified=self.fs.files_modified,
            commands_executed=cmd_history,
            errors=self.errors,
            summary=summary,
            unified_diff=diff,
        )
