"""Create a draft pull request from an agent run's patch and report.

This module is used by the GitHub Action entrypoint when the ``create_pr``
input is enabled.  It shells out to ``git`` and ``gh`` (GitHub CLI) which
are available in the Actions runner environment, and falls back to the
GitHub REST API via ``httpx`` when ``gh`` is not present.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)


def _run(cmd: list[str], cwd: str | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run a subprocess command with logging."""
    logger.debug("Running: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd, timeout=120)
    if check and result.returncode != 0:
        logger.error("Command failed (exit %d): %s\nstderr: %s", result.returncode, " ".join(cmd), result.stderr)
        raise subprocess.CalledProcessError(result.returncode, cmd, result.stdout, result.stderr)
    return result


def create_pr(
    *,
    repo_root: str,
    patch_path: str,
    report_path: str,
    task_description: str,
    github_token: str | None = None,
    base_branch: str = "main",
    max_diff_lines: int = 3000,
) -> str | None:
    """Create a draft PR from a patch file.

    Returns the PR URL on success, or ``None`` if the patch is empty,
    oversized, or creation fails.

    Parameters
    ----------
    max_diff_lines:
        Hard limit on added + removed lines. Patches exceeding this are
        rejected with a warning so accidentally-large diffs (e.g. generated
        files included in the patch) don't open noisy PRs. Default: 3000.
    """
    token = github_token or os.environ.get("GITHUB_TOKEN", "")
    if not token:
        logger.error("No GITHUB_TOKEN available; cannot create PR.")
        return None

    patch = Path(patch_path)
    if not patch.exists() or patch.stat().st_size == 0:
        logger.info("Patch file is empty or missing; skipping PR creation.")
        return None

    # Pre-flight: reject oversized patches before touching the working tree
    size = estimate_patch_size(patch_path)
    if size["total_diff"] > max_diff_lines:
        logger.warning(
            "Patch is oversized (%d lines changed across %d files, limit %d). "
            "Skipping PR creation — split the patch or raise max_diff_lines.",
            size["total_diff"],
            size["files"],
            max_diff_lines,
        )
        return None

    # Read report for the PR body
    report_body = ""
    report_file = Path(report_path)
    if report_file.exists():
        try:
            report_data = json.loads(report_file.read_text(encoding="utf-8"))
            report_body = _format_pr_body(report_data, task_description)
        except Exception as exc:
            logger.warning("Could not read report for PR body: %s", exc)
            report_body = f"## Agent Sandbox Run\n\nTask: {task_description}\n\n_Report could not be parsed._"
    else:
        report_body = f"## Agent Sandbox Run\n\nTask: {task_description}"

    # Create branch
    timestamp = int(time.time())
    branch_name = f"agent-sandbox/run-{timestamp}"

    try:
        _run(["git", "checkout", "-b", branch_name], cwd=repo_root)
    except subprocess.CalledProcessError:
        logger.error("Failed to create branch %s", branch_name)
        return None

    # Apply the patch
    try:
        _run(["git", "apply", "--check", str(patch.resolve())], cwd=repo_root)
        _run(["git", "apply", str(patch.resolve())], cwd=repo_root)
    except subprocess.CalledProcessError as exc:
        logger.error("Failed to apply patch: %s", exc.stderr)
        _run(["git", "checkout", "-"], cwd=repo_root, check=False)
        return None

    # Stage and commit
    _run(["git", "add", "-A"], cwd=repo_root)

    commit_msg = f"Agent Sandbox: {task_description[:72]}"
    try:
        _run(["git", "commit", "-m", commit_msg], cwd=repo_root)
    except subprocess.CalledProcessError:
        logger.error("Nothing to commit after applying patch.")
        _run(["git", "checkout", "-"], cwd=repo_root, check=False)
        return None

    # Push branch
    try:
        _run(["git", "push", "-u", "origin", branch_name], cwd=repo_root)
    except subprocess.CalledProcessError as exc:
        logger.error("Failed to push branch: %s", exc.stderr)
        return None

    # Create draft PR
    pr_title = f"Agent Sandbox: {task_description[:60]}"
    return _create_draft_pr(
        token=token,
        repo_root=repo_root,
        branch_name=branch_name,
        base_branch=base_branch,
        title=pr_title,
        body=report_body,
    )


def _format_pr_body(report: dict, task_description: str) -> str:
    """Format the JSON report into a markdown PR body."""
    lines = [
        "## Agent Sandbox Run",
        "",
        f"**Task:** {task_description}",
        "",
        f"**Summary:** {report.get('summary', 'N/A')}",
        "",
    ]

    files = report.get("files_modified", [])
    if files:
        lines.append("### Files Modified")
        for f in files:
            lines.append(f"- `{f}`")
        lines.append("")

    errors = report.get("errors", [])
    if errors:
        lines.append("### Errors")
        for e in errors:
            lines.append(f"- {e}")
        lines.append("")

    cmds = report.get("commands_executed", [])
    if cmds:
        lines.append("### Commands Executed")
        for c in cmds:
            exit_code = c.get("exit_code", "?")
            lines.append(f"- `{c.get('command', '?')}` (exit {exit_code})")
        lines.append("")

    lines.append("---")
    lines.append("_This PR was created automatically by Agent Sandbox._")
    return "\n".join(lines)


def _create_draft_pr(
    *,
    token: str,
    repo_root: str,
    branch_name: str,
    base_branch: str,
    title: str,
    body: str,
) -> str | None:
    """Create a draft PR using the GitHub REST API."""
    # Determine repo owner/name from git remote
    try:
        result = _run(["git", "remote", "get-url", "origin"], cwd=repo_root)
        remote_url = result.stdout.strip()
    except subprocess.CalledProcessError:
        logger.error("Could not determine remote URL.")
        return None

    owner, repo = _parse_github_remote(remote_url)
    if not owner or not repo:
        logger.error("Could not parse owner/repo from remote URL: %s", remote_url)
        return None

    api_url = f"https://api.github.com/repos/{owner}/{repo}/pulls"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    payload = {
        "title": title,
        "body": body,
        "head": branch_name,
        "base": base_branch,
        "draft": True,
    }

    try:
        with httpx.Client(timeout=30) as client:
            resp = client.post(api_url, headers=headers, json=payload)
            resp.raise_for_status()
            pr_data = resp.json()
            pr_url = pr_data.get("html_url", "")
            logger.info("Created draft PR: %s", pr_url)
            return pr_url
    except Exception as exc:
        logger.error("Failed to create PR via API: %s", exc)
        return None


def estimate_patch_size(patch_path: str) -> dict:
    """Return line-count statistics for a patch file.

    Useful as a pre-flight check before opening a PR — very large diffs
    (e.g. accidentally including generated files) are flagged so the caller
    can decide whether to split or abort.

    Returns a dict with keys:
        added       – number of lines starting with '+'
        removed     – number of lines starting with '-'
        total_diff  – added + removed
        files       – number of '--- a/' hunks (approximate file count)
        oversized   – True when total_diff > 2000
    """
    p = Path(patch_path)
    if not p.exists():
        return {"added": 0, "removed": 0, "total_diff": 0, "files": 0, "oversized": False}

    added = removed = files = 0
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
        elif line.startswith("--- a/"):
            files += 1

    total_diff = added + removed
    return {
        "added": added,
        "removed": removed,
        "total_diff": total_diff,
        "files": files,
        "oversized": total_diff > 2000,
    }


def _parse_github_remote(url: str) -> tuple[str, str]:
    """Extract (owner, repo) from a GitHub remote URL.

    Handles both HTTPS and SSH formats:
    - https://github.com/owner/repo.git
    - git@github.com:owner/repo.git
    """
    url = url.strip()
    if url.startswith("git@"):
        # git@github.com:owner/repo.git
        _, path = url.split(":", 1)
        path = path.removesuffix(".git")
        parts = path.split("/")
        if len(parts) >= 2:
            return parts[-2], parts[-1]
    elif "github.com" in url:
        # https://github.com/owner/repo.git
        url = url.removesuffix(".git")
        parts = url.rstrip("/").split("/")
        if len(parts) >= 2:
            return parts[-2], parts[-1]
    return "", ""
