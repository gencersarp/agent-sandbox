"""Tests for manifest parsing and validation."""

import textwrap
from pathlib import Path

import pytest

from src.manifest import Manifest, ManifestError, load_manifest


@pytest.fixture
def tmp_manifest(tmp_path: Path):
    """Helper that writes YAML text to a temp file and returns the path."""

    def _write(content: str) -> Path:
        p = tmp_path / ".agent-sandbox.yml"
        p.write_text(textwrap.dedent(content), encoding="utf-8")
        return p

    return _write


# ------------------------------------------------------------------
# Happy path
# ------------------------------------------------------------------

class TestValidManifests:
    def test_minimal(self, tmp_manifest):
        path = tmp_manifest("""\
            agent_task:
              description: "Do something useful"
        """)
        m = load_manifest(path)
        assert m.agent_task.description == "Do something useful"
        assert m.allowed_paths.read_only == []
        assert m.allowed_paths.read_write == []
        assert m.allowed_commands == []
        assert m.network.allowed_domains == []

    def test_full(self, tmp_manifest):
        path = tmp_manifest("""\
            allowed_paths:
              read_only:
                - "**/*.py"
                - "pyproject.toml"
              read_write:
                - "src/**"
            allowed_commands:
              - "ruff check .*"
              - "pytest.*"
            network:
              allowed_domains:
                - "api.openai.com"
                - "*.example.com"
            agent_task:
              description: "Fix lint issues"
              instructions: "Use ruff only"
        """)
        m = load_manifest(path)
        assert m.allowed_paths.read_only == ["**/*.py", "pyproject.toml"]
        assert m.allowed_paths.read_write == ["src/**"]
        assert m.allowed_commands == ["ruff check .*", "pytest.*"]
        assert m.network.allowed_domains == ["api.openai.com", "*.example.com"]
        assert m.agent_task.description == "Fix lint issues"
        assert m.agent_task.instructions == "Use ruff only"

    def test_single_string_coerced_to_list(self, tmp_manifest):
        path = tmp_manifest("""\
            allowed_paths:
              read_only: "*.py"
            allowed_commands: "echo hello"
            agent_task:
              description: "test"
        """)
        m = load_manifest(path)
        assert m.allowed_paths.read_only == ["*.py"]
        assert m.allowed_commands == ["echo hello"]

    def test_all_readable_globs(self, tmp_manifest):
        path = tmp_manifest("""\
            allowed_paths:
              read_only: ["a/*"]
              read_write: ["b/*"]
            agent_task:
              description: "test"
        """)
        m = load_manifest(path)
        assert m.all_readable_globs == ["a/*", "b/*"]
        assert m.all_writable_globs == ["b/*"]

    def test_only_required_fields(self, tmp_manifest):
        """Manifest with only the required agent_task field."""
        path = tmp_manifest("""\
            agent_task:
              description: "Minimal task"
        """)
        m = load_manifest(path)
        assert m.agent_task.description == "Minimal task"
        assert m.agent_task.instructions is None
        assert m.allowed_paths.read_only == []
        assert m.allowed_paths.read_write == []
        assert m.allowed_commands == []
        assert m.network.allowed_domains == []

    def test_all_optional_fields(self, tmp_manifest):
        """Manifest with every optional field populated."""
        path = tmp_manifest("""\
            allowed_paths:
              read_only:
                - "**/*.py"
                - "**/*.md"
                - "*.toml"
              read_write:
                - "src/**"
                - "tests/**"
            allowed_commands:
              - "ruff .*"
              - "pytest .*"
              - "mypy .*"
            network:
              allowed_domains:
                - "api.openai.com"
                - "*.github.com"
                - "pypi.org"
            agent_task:
              description: "Full manifest test"
              instructions: "Be thorough and careful."
        """)
        m = load_manifest(path)
        assert len(m.allowed_paths.read_only) == 3
        assert len(m.allowed_paths.read_write) == 2
        assert len(m.allowed_commands) == 3
        assert len(m.network.allowed_domains) == 3
        assert m.agent_task.instructions == "Be thorough and careful."

    def test_deeply_nested_paths(self, tmp_manifest):
        """Manifest with deeply nested path patterns."""
        path = tmp_manifest("""\
            allowed_paths:
              read_only:
                - "a/b/c/d/e/**/*.py"
              read_write:
                - "src/deep/nested/module/**"
            agent_task:
              description: "Deep paths"
        """)
        m = load_manifest(path)
        assert "a/b/c/d/e/**/*.py" in m.allowed_paths.read_only
        assert "src/deep/nested/module/**" in m.allowed_paths.read_write

    def test_regex_command_patterns(self, tmp_manifest):
        """Manifest with complex regex patterns in allowed_commands."""
        path = tmp_manifest("""\
            allowed_commands:
              - "^ruff (check|format) --fix \\\\."
              - "^pytest( -v)?( --tb=short)?$"
              - "^echo [a-zA-Z0-9_ ]+$"
            agent_task:
              description: "Regex patterns"
        """)
        m = load_manifest(path)
        assert len(m.allowed_commands) == 3

    def test_empty_allowed_paths(self, tmp_manifest):
        """Explicit empty lists for allowed_paths."""
        path = tmp_manifest("""\
            allowed_paths:
              read_only: []
              read_write: []
            agent_task:
              description: "No paths"
        """)
        m = load_manifest(path)
        assert m.allowed_paths.read_only == []
        assert m.allowed_paths.read_write == []


# ------------------------------------------------------------------
# Error cases
# ------------------------------------------------------------------

class TestInvalidManifests:
    def test_file_not_found(self):
        with pytest.raises(ManifestError, match="not found"):
            load_manifest("/nonexistent/.agent-sandbox.yml")

    def test_empty_file(self, tmp_manifest):
        path = tmp_manifest("")
        with pytest.raises(ManifestError, match="empty"):
            load_manifest(path)

    def test_invalid_yaml(self, tmp_manifest):
        path = tmp_manifest(":::bad yaml:::")
        with pytest.raises(ManifestError, match="Invalid YAML"):
            load_manifest(path)

    def test_not_a_mapping(self, tmp_manifest):
        path = tmp_manifest("- a\n- b\n")
        with pytest.raises(ManifestError, match="mapping"):
            load_manifest(path)

    def test_missing_agent_task(self, tmp_manifest):
        path = tmp_manifest("""\
            allowed_commands: ["echo hi"]
        """)
        with pytest.raises(ManifestError, match="agent_task"):
            load_manifest(path)

    def test_empty_description(self, tmp_manifest):
        path = tmp_manifest("""\
            agent_task:
              description: ""
        """)
        with pytest.raises(ManifestError):
            load_manifest(path)

    def test_invalid_regex_in_commands(self, tmp_manifest):
        path = tmp_manifest("""\
            allowed_commands:
              - "[invalid(regex"
            agent_task:
              description: "test"
        """)
        with pytest.raises(ManifestError, match="Invalid regex"):
            load_manifest(path)

    def test_directory_instead_of_file(self, tmp_path):
        with pytest.raises(ManifestError, match="not a file"):
            load_manifest(tmp_path)

    def test_yaml_with_tabs(self, tmp_manifest):
        """YAML with tabs instead of spaces should fail or be handled."""
        path = tmp_manifest("agent_task:\n\tdescription: 'tab indented'")
        # PyYAML rejects tabs in indentation
        with pytest.raises(ManifestError):
            load_manifest(path)

    def test_whitespace_only_file(self, tmp_manifest):
        path = tmp_manifest("   \n  \n  ")
        with pytest.raises(ManifestError, match="empty"):
            load_manifest(path)

    def test_empty_manifest_dict(self, tmp_manifest):
        """A valid YAML dict but with no known keys."""
        path = tmp_manifest("""\
            unknown_key: "value"
        """)
        with pytest.raises(ManifestError):
            load_manifest(path)
