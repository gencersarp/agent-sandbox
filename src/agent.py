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
    fetches: list[dict[str, Any]]
    comments: list[dict[str, Any]]
    list_dirs: list[dict[str, Any]]  # New: list of {path, files}
    errors: list[str]
    summary: str
    unified_diff: str

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(
            {
                "files_modified": self.files_modified,
                "commands_executed": self.commands_executed,
                "fetches": self.fetches,
                "comments": self.comments,
                "list_dirs": self.list_dirs,
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

4. Comment on a specific line of a file:
   {"action": "comment", "path": "src/main.py", "line": 10, "text": "This could be optimized."}

5. Fetch content from a URL:
   {"action": "fetch", "url": "https://example.com/api/v1/data"}

6. List files in a directory:
   {"action": "list_dir", "path": "src"}

7. Run a git command (e.g., status, diff, log):
   {"action": "git", "subcommand": "status"}

EXAMPLE — a complete valid response:
[
  {"action": "list_dir", "path": "."},
  {"action": "read", "path": "src/app.py"},
  {"action": "comment", "path": "src/app.py", "line": 5, "text": "Consider adding a docstring."},
  {"action": "write", "path": "src/app.py", "content": "# updated\\nimport sys\\n"},
  {"action": "git", "subcommand": "diff"},
  {"action": "run", "command": "echo done"},
  {"action": "fetch", "url": "https://example.com/status"}
]

Respond ONLY with a JSON array of step objects.  No markdown fences, no commentary.
If you have completed the task and have no further actions to take, respond with an empty array: []
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
    # Filter file list to only show what the agent can actually see
    # and isn't explicitly ignored.
    fs_helper = SandboxedFileSystem(manifest, ctx.root)
    allowed_files = [
        f for f in ctx.file_list
        if fs_helper._can_read(f)
    ]

    parts = [
        f"## Task\n{manifest.agent_task.description}",
    ]
    if manifest.agent_task.instructions:
        parts.append(f"## Extra instructions\n{manifest.agent_task.instructions}")
    parts.append(f"## Allowed readable globs\n{manifest.all_readable_globs}")
    parts.append(f"## Allowed writable globs\n{manifest.all_writable_globs}")
    parts.append(f"## Allowed commands\n{manifest.allowed_commands}")
    if manifest.meta.ignore:
        parts.append(f"## Always ignored globs\n{manifest.meta.ignore}")

    if allowed_files:
        if len(allowed_files) <= 100:
            parts.append("## Repo file list (filtered)\n" + "\n".join(allowed_files))
        else:
            # Show a sampled/truncated list and advise using list_dir
            truncated = allowed_files[:100]
            parts.append(
                f"## Repo file list (filtered & truncated, total {len(allowed_files)} relevant files)\n"
                + "\n".join(truncated)
                + "\n\n(Use 'list_dir' to explore specific directories if needed)"
            )
    else:
        parts.append("## Repo file list\n(No files matching allowed readable patterns were found)")

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
        self.comments: list[dict[str, Any]] = []
        self.fetches: list[dict[str, Any]] = []
        self.list_dirs: list[dict[str, Any]] = []
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

    def _exec_step(self, step: dict[str, Any]) -> dict[str, Any] | None:
        action = step.get("action")
        result_data: dict[str, Any] = {"action": action}
        
        if action == "read":
            path = step.get("path", "")
            logger.info("READ  %s", path)
            try:
                content = self.fs.read(path)
                logger.debug("Read %d bytes from %s", len(content), path)
                result_data["status"] = "success"
                result_data["content"] = content
            except (SandboxViolationError, FileNotFoundError) as exc:
                err_msg = str(exc)
                self.errors.append(f"read {path}: {err_msg}")
                result_data["status"] = "error"
                result_data["message"] = err_msg
        elif action == "write":
            path = step.get("path", "")
            content = step.get("content", "")
            logger.info("WRITE %s (%d bytes)", path, len(content))
            try:
                self._snapshot_original(path)
                self.fs.write(path, content)
                result_data["status"] = "success"
            except SandboxViolationError as exc:
                err_msg = str(exc)
                self.errors.append(f"write {path}: {err_msg}")
                result_data["status"] = "error"
                result_data["message"] = err_msg
        elif action == "run":
            command = step.get("command", "")
            logger.info("RUN   %s", command)
            try:
                res = self.cmd.run(command)
                result_data["exit_code"] = res.exit_code
                result_data["stdout"] = res.stdout
                result_data["stderr"] = res.stderr
                if not res.ok:
                    self.errors.append(
                        f"command failed (exit {res.exit_code}): {command}\n"
                        f"stderr: {res.stderr[:500]}"
                    )
            except SandboxViolationError as exc:
                err_msg = str(exc)
                self.errors.append(f"run {command}: {err_msg}")
                result_data["status"] = "error"
                result_data["message"] = err_msg
        elif action == "comment":
            path = step.get("path", "")
            line = step.get("line")
            text = step.get("text", "")
            logger.info("COMMENT %s:%s %s", path, line, text[:30])
            self.comments.append({"path": path, "line": line, "text": text})
            result_data["status"] = "success"
        elif action == "fetch":
            url = step.get("url", "")
            logger.info("FETCH %s", url)
            try:
                self.llm.network_guard.check_url(url)
                with httpx.Client(timeout=30) as client:
                    resp = client.get(url)
                    resp.raise_for_status()
                    content = resp.text
                    self.fetches.append({
                        "url": url,
                        "status_code": resp.status_code,
                        "content_excerpt": content[:1000]
                    })
                    result_data["status"] = "success"
                    result_data["content"] = content
            except Exception as exc:
                err_msg = str(exc)
                self.errors.append(f"fetch {url}: {err_msg}")
                result_data["status"] = "error"
                result_data["message"] = err_msg
        elif action == "list_dir":
            path = step.get("path", ".")
            logger.info("LIST_DIR %s", path)
            try:
                files = self.fs.list_files(path)
                logger.info("Found %d files in %s", len(files), path)
                self.list_dirs.append({"path": path, "files": files})
                result_data["status"] = "success"
                result_data["files"] = files
            except (SandboxViolationError, FileNotFoundError) as exc:
                err_msg = str(exc)
                self.errors.append(f"list_dir {path}: {err_msg}")
                result_data["status"] = "error"
                result_data["message"] = err_msg
        elif action == "git":
            subcommand = step.get("subcommand", "status")
            logger.info("GIT %s", subcommand)
            try:
                res = self.cmd.run_git(subcommand)
                result_data["exit_code"] = res.exit_code
                result_data["stdout"] = res.stdout
                result_data["stderr"] = res.stderr
                if not res.ok:
                    self.errors.append(
                        f"git {subcommand} failed (exit {res.exit_code})\n"
                        f"stderr: {res.stderr[:500]}"
                    )
            except SandboxViolationError as exc:
                err_msg = str(exc)
                self.errors.append(f"git {subcommand}: {err_msg}")
                result_data["status"] = "error"
                result_data["message"] = err_msg
        else:
            err_msg = f"Unknown action: {action!r}"
            self.errors.append(err_msg)
            result_data["status"] = "error"
            result_data["message"] = err_msg

        return result_data

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

    def run(self, max_turns: int = 5) -> AgentReport:
        """Execute the full agent loop: plan via LLM, execute, report.
        
        Supports multi-turn execution where the agent can react to outputs.
        """
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": _build_user_prompt(self.manifest, self.ctx)},
        ]

        for turn in range(max_turns):
            logger.info("--- Turn %d/%d ---", turn + 1, max_turns)
            
            # 1. Ask the LLM for next steps
            try:
                raw_plan = self.llm.chat(messages)
            except Exception as exc:
                if turn > 0:
                    # Likely end of conversation or mock exhausted in tests
                    logger.info("LLM chat ended after %d turns: %s", turn, exc)
                else:
                    self.errors.append(f"LLM request failed: {exc}")
                break

            messages.append({"role": "assistant", "content": raw_plan})

            # 2. Parse steps
            try:
                steps = _parse_steps(raw_plan)
            except ValueError as exc:
                if turn > 0:
                    # If it's not JSON after the first turn, it might be the agent
                    # just giving a natural language conclusion (even if discouraged).
                    logger.info("LLM provided non-JSON response after turn 1. Ending loop.")
                    break
                
                self.errors.append(str(exc))
                # Try to tell the LLM it messed up the JSON
                messages.append({
                    "role": "user", 
                    "content": f"Error parsing your JSON: {exc}. Please respond with a valid JSON array of steps."
                })
                continue

            if not steps:
                logger.info("No more steps from LLM. Ending run.")
                break

            # 3. Execute steps and gather feedback
            turn_results = []
            for i, step in enumerate(steps):
                logger.info("Step %d/%d: %s", i + 1, len(steps), step.get("action"))
                res = self._exec_step(step)
                if res:
                    turn_results.append(res)

            # 4. Feed back to LLM
            if turn_results:
                messages.append({
                    "role": "user",
                    "content": f"Results from turn {turn + 1}:\n" + json.dumps(turn_results, indent=2)
                })
            else:
                messages.append({"role": "user", "content": "Steps executed successfully. Any further actions?"})

        # 5. Finalize report
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

        report_data = {
            "files_modified": self.fs.files_modified,
            "commands_executed": cmd_history,
            "comments": self.comments,
            "errors": self.errors,
        }
        llm_summary = self._generate_llm_summary(report_data)
        summary = llm_summary if llm_summary else basic_summary

        return AgentReport(
            files_modified=self.fs.files_modified,
            commands_executed=cmd_history,
            fetches=self.fetches,
            comments=self.comments,
            list_dirs=self.list_dirs,
            errors=self.errors,
            summary=summary,
            unified_diff=diff,
        )

