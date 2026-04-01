"""CLI entry point for agent-sandbox."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import click

from .agent import AgentRunner, LLMConfig, RepoContext
from .logging_config import setup_logging
from .manifest import ManifestError, load_manifest

logger = logging.getLogger(__name__)


def _collect_file_list(root: Path) -> list[str]:
    """Use ``git ls-files`` to get a fast file list, falling back to os.walk."""
    try:
        result = subprocess.run(
            ["git", "ls-files"],
            capture_output=True,
            text=True,
            cwd=root,
            timeout=30,
        )
        if result.returncode == 0:
            return [f for f in result.stdout.strip().splitlines() if f]
    except Exception:
        pass

    # Fallback: walk the directory (limit depth to avoid huge trees)
    files: list[str] = []
    for dirpath, _dirs, fnames in os.walk(root):
        for fn in fnames:
            full = Path(dirpath) / fn
            try:
                rel = str(full.relative_to(root))
            except ValueError:
                continue
            files.append(rel)
            if len(files) > 5000:
                return files
    return files


def _git_diff_summary(root: Path, base_branch: str | None = None) -> str:
    """Return a short git diff summary.

    If *base_branch* is given, diff against that branch.  Otherwise diff
    ``HEAD~1`` to show the most recent commit's changes.  Returns an empty
    string if git is unavailable or the repo has no history.
    """
    try:
        if base_branch:
            cmd = ["git", "diff", "--stat", base_branch]
        else:
            cmd = ["git", "diff", "--stat", "HEAD~1"]
        result = subprocess.run(cmd, capture_output=True, text=True, cwd=root, timeout=30)
        if result.returncode == 0:
            return result.stdout.strip()
        # HEAD~1 may fail on initial commit; fall back to plain diff
        if not base_branch:
            result = subprocess.run(
                ["git", "diff", "--stat"],
                capture_output=True, text=True, cwd=root, timeout=30,
            )
            return result.stdout.strip() if result.returncode == 0 else ""
        return ""
    except Exception:
        return ""


@click.command("agent-sandbox")
@click.option(
    "--manifest",
    "-m",
    "manifest_path",
    default=".agent-sandbox.yml",
    show_default=True,
    help="Path to the manifest YAML file.",
)
@click.option(
    "--base-branch",
    default=None,
    help="Base branch for git diff context (default: diff against HEAD~1).",
)
@click.option(
    "--repo-root",
    default=".",
    show_default=True,
    help="Root of the repository to operate on.",
)
@click.option(
    "--api-url",
    default=None,
    help="LLM API URL (default: env AGENT_SANDBOX_API_URL or OpenAI).",
)
@click.option(
    "--api-key",
    default=None,
    help="LLM API key (default: env AGENT_SANDBOX_API_KEY).",
)
@click.option(
    "--model",
    default=None,
    help="LLM model name (default: env AGENT_SANDBOX_MODEL or gpt-4o).",
)
@click.option("--output", "-o", default=None, help="Write JSON report to this file.")
@click.option("--patch", default=None, help="Write unified diff patch to this file.")
@click.option("-v", "--verbose", is_flag=True, help="Verbose logging.")
def main(
    manifest_path: str,
    base_branch: str | None,
    repo_root: str,
    api_url: str | None,
    api_key: str | None,
    model: str | None,
    output: str | None,
    patch: str | None,
    verbose: bool,
) -> None:
    """Run the agent sandbox on a repository."""
    setup_logging(verbose=verbose)

    root = Path(repo_root).resolve()
    manifest_file = root / manifest_path if not Path(manifest_path).is_absolute() else Path(manifest_path)

    # Load manifest
    try:
        manifest = load_manifest(manifest_file)
    except ManifestError as exc:
        logger.error("%s", exc)
        sys.exit(1)

    logger.info("Loaded manifest from %s", manifest_file)
    logger.info("Task: %s", manifest.agent_task.description)

    # Build repo context
    file_list = _collect_file_list(root)
    diff_summary = _git_diff_summary(root, base_branch)
    ctx = RepoContext(root=root, file_list=file_list, git_diff_summary=diff_summary)
    logger.info("Repo context: %d files, diff summary %d chars", len(file_list), len(diff_summary))

    # LLM config
    llm_cfg = LLMConfig(
        api_url=api_url or os.environ.get("AGENT_SANDBOX_API_URL", LLMConfig.api_url),
        api_key=api_key or os.environ.get("AGENT_SANDBOX_API_KEY", ""),
        model=model or os.environ.get("AGENT_SANDBOX_MODEL", LLMConfig.model),
    )
    if not llm_cfg.api_key:
        logger.error("No LLM API key provided. Use --api-key or AGENT_SANDBOX_API_KEY env var.")
        sys.exit(1)

    # Run agent
    runner = AgentRunner(manifest, ctx, llm_cfg)
    report = runner.run()

    # Output
    click.echo("\n" + "=" * 60)
    click.echo(report.summary)
    click.echo("=" * 60)

    if report.errors:
        click.echo("\nErrors:")
        for err in report.errors:
            click.echo(f"  - {err}")

    if report.files_modified:
        click.echo("\nFiles modified:")
        for f in report.files_modified:
            click.echo(f"  {f}")

    if report.unified_diff:
        click.echo("\n--- Unified Diff ---")
        click.echo(report.unified_diff)

    if output:
        Path(output).write_text(report.to_json(), encoding="utf-8")
        logger.info("Report written to %s", output)

    if patch and report.unified_diff:
        Path(patch).write_text(report.unified_diff, encoding="utf-8")
        logger.info("Patch written to %s", patch)

    if report.errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
