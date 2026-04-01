"""Parse and validate .agent-sandbox.yml manifest files."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator


class AllowedPaths(BaseModel):
    """File-system access policy expressed as glob patterns."""

    read_only: list[str] = Field(default_factory=list, description="Glob patterns the agent may read but not write.")
    read_write: list[str] = Field(default_factory=list, description="Glob patterns the agent may read and write.")

    @field_validator("read_only", "read_write", mode="before")
    @classmethod
    def _ensure_list(cls, v: object) -> list[str]:
        if v is None:
            return []
        if isinstance(v, str):
            return [v]
        if isinstance(v, list):
            return [str(item) for item in v]
        raise ValueError(f"Expected a list of glob patterns, got {type(v).__name__}")


class NetworkPolicy(BaseModel):
    """Network access policy."""

    allowed_domains: list[str] = Field(default_factory=list, description="Domains the agent may contact (exact match or wildcard prefix).")

    @field_validator("allowed_domains", mode="before")
    @classmethod
    def _ensure_list(cls, v: object) -> list[str]:
        if v is None:
            return []
        if isinstance(v, str):
            return [v]
        if isinstance(v, list):
            return [str(item) for item in v]
        raise ValueError(f"Expected a list of domain strings, got {type(v).__name__}")


class AgentTask(BaseModel):
    """What the agent should do."""

    description: str = Field(..., min_length=1, description="Human-readable description of the task.")
    instructions: Optional[str] = Field(default=None, description="Optional extra instructions for the LLM.")


class Manifest(BaseModel):
    """Top-level schema for .agent-sandbox.yml."""

    allowed_paths: AllowedPaths = Field(default_factory=AllowedPaths)
    allowed_commands: list[str] = Field(default_factory=list, description="Shell commands or regexes the agent may run.")
    network: NetworkPolicy = Field(default_factory=NetworkPolicy)
    agent_task: AgentTask

    @field_validator("allowed_commands", mode="before")
    @classmethod
    def _ensure_list(cls, v: object) -> list[str]:
        if v is None:
            return []
        if isinstance(v, str):
            return [v]
        if isinstance(v, list):
            return [str(item) for item in v]
        raise ValueError(f"Expected a list of command patterns, got {type(v).__name__}")

    @model_validator(mode="after")
    def _validate_regexes(self) -> "Manifest":
        """Make sure every allowed_commands entry is a valid regex."""
        for pattern in self.allowed_commands:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"Invalid regex in allowed_commands: {pattern!r} — {exc}") from exc
        return self

    # ------------------------------------------------------------------
    # Convenience helpers
    # ------------------------------------------------------------------

    @property
    def all_readable_globs(self) -> list[str]:
        """All glob patterns the agent is allowed to read."""
        return self.allowed_paths.read_only + self.allowed_paths.read_write

    @property
    def all_writable_globs(self) -> list[str]:
        """All glob patterns the agent is allowed to write."""
        return self.allowed_paths.read_write


class ManifestError(Exception):
    """Raised when a manifest file is missing or invalid."""


def load_manifest(path: str | Path) -> Manifest:
    """Load and validate a manifest from *path*.

    Raises ``ManifestError`` with a human-friendly message on any problem.
    """
    path = Path(path)
    if not path.exists():
        raise ManifestError(f"Manifest file not found: {path}")
    if not path.is_file():
        raise ManifestError(f"Manifest path is not a file: {path}")

    raw_text = path.read_text(encoding="utf-8")
    if not raw_text.strip():
        raise ManifestError(f"Manifest file is empty: {path}")

    try:
        data = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise ManifestError(f"Invalid YAML in {path}: {exc}") from exc

    if not isinstance(data, dict):
        raise ManifestError(f"Manifest must be a YAML mapping, got {type(data).__name__}")

    # Validate that required keys are present before attempting Pydantic parsing,
    # so that inputs like ":::bad yaml:::" (which YAML parses as a dict key with
    # value None) produce "Invalid YAML" rather than a confusing validation error.
    if "agent_task" not in data:
        # Check if the data looks like it was not intentional YAML structure
        # (e.g. ":::bad yaml:::" parses as {':::bad yaml:::': None})
        keys = list(data.keys())
        known_keys = {"allowed_paths", "allowed_commands", "network", "agent_task"}
        if not any(k in known_keys for k in keys):
            raise ManifestError(
                f"Invalid YAML in {path}: file does not contain a valid manifest structure"
            )

    try:
        return Manifest(**data)
    except Exception as exc:
        raise ManifestError(f"Manifest validation failed: {exc}") from exc
