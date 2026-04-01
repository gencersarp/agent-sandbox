# Agent Sandbox for GitHub Actions

A framework and GitHub Action that runs LLM agents safely inside CI with strict capability manifests, resource limits, and explicit human approval of proposed changes.

## Architecture

```
.agent-sandbox.yml          CLI / GitHub Action
  (manifest)                       |
       |                     +-----v------+
       +-------------------->| ManifestParser |
                             +-----+------+
                                   |
                    +--------------v--------------+
                    |        AgentRunner           |
                    |  1. Build prompt from task   |
                    |  2. Call LLM for plan (JSON) |
                    |  3. Execute steps through    |
                    |     sandboxed layer          |
                    |  4. Generate diff + report   |
                    |  5. Summarize via LLM        |
                    +--------------+--------------+
                                   |
              +--------------------+--------------------+
              |                    |                    |
    +---------v--------+ +--------v--------+ +--------v--------+
    | SandboxedFileSystem| |SandboxedCommand | |  NetworkGuard   |
    | - glob matching   | | Runner          | | - domain allow  |
    | - path traversal  | | - regex allow   | |   list          |
    |   prevention      | |   list          | | - wildcard      |
    | - read/write      | | - timeout       | |   support       |
    |   enforcement     | | - history       | |                 |
    +---------+--------+ +--------+--------+ +--------+--------+
              |                    |                    |
              v                    v                    v
         File I/O           Subprocess            HTTP validation
    (repo root only)     (allowlisted only)    (allowlisted domains)
```

## How It Works

1. You define an `.agent-sandbox.yml` manifest in your repo that declares exactly what the agent is allowed to do: which files it can read/write, which commands it can run, and which network domains it can contact.
2. The agent runner loads the manifest, sends the task and repo context to an LLM, and executes the LLM's plan through a sandboxed layer that enforces the manifest policy.
3. All file modifications are captured as a unified diff patch. A JSON report summarizes what happened, including an LLM-generated summary.
4. In CI, the patch and report are uploaded as artifacts for human review before merging.
5. Optionally, a draft PR can be created automatically with the changes.

## Security Model

### File-system Isolation

The agent can only read files matching `allowed_paths.read_only` or `allowed_paths.read_write` globs. It can only write to paths matching `read_write` globs. Path traversal (`../`) is blocked -- all paths are resolved and checked against the repo root. Symlinks pointing outside the repo root are also blocked.

Glob patterns support:
- `**/*.py` -- matches `.py` files at any depth
- `*.txt` -- matches `.txt` files only at the root
- `src/**` -- matches any file under `src/`
- `src/**/*.py` -- matches `.py` files anywhere under `src/`

### Command Allowlisting

Only commands matching regexes in `allowed_commands` can be executed. Everything else raises a `SandboxViolationError`. Commands run with timeouts to prevent hangs.

### Network Restriction

Outbound HTTP requests are validated against `network.allowed_domains`. Wildcards like `*.example.com` are supported. The LLM API domain must be in the allowlist.

### No Auto-merge

The action produces a patch artifact. A human must review and apply it (or use the optional draft PR feature).

### Retry Logic

The LLM client retries transient failures (HTTP 429, 500, 502, 503, 504, connection errors, timeouts) with exponential backoff: 1s, 2s, 4s delays across 3 attempts.

## Manifest Schema

```yaml
# .agent-sandbox.yml

allowed_paths:
  read_only:
    - "**/*.py"           # Read any Python file
    - "pyproject.toml"    # Read project config
  read_write:
    - "src/**"            # Read and write anything under src/

allowed_commands:
  - "ruff check --fix .*"  # Regex: ruff with --fix flag
  - "pytest.*"              # Regex: any pytest invocation

network:
  allowed_domains:
    - "api.openai.com"      # Exact domain match
    - "*.example.com"       # Wildcard: any subdomain

agent_task:
  description: "Fix lint issues across the codebase."
  instructions: "Use ruff only. Do not change logic."  # Optional
```

### Field Reference

| Field | Required | Type | Description |
|-------|----------|------|-------------|
| `allowed_paths.read_only` | No | `list[str]` | Glob patterns the agent may read but not write. |
| `allowed_paths.read_write` | No | `list[str]` | Glob patterns the agent may read and write. |
| `allowed_commands` | No | `list[str]` | Regex patterns for allowed shell commands. |
| `network.allowed_domains` | No | `list[str]` | Domains the agent may contact (exact or `*.` wildcard). |
| `agent_task.description` | **Yes** | `str` | Human-readable description of the task. |
| `agent_task.instructions` | No | `str` | Extra instructions for the LLM. |

## Usage

### As a GitHub Action

```yaml
name: Agent Sandbox
on:
  pull_request:
    types: [opened, synchronize]

permissions:
  contents: write
  pull-requests: write

jobs:
  agent-run:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - name: Run Agent Sandbox
        id: agent
        uses: your-org/agent-sandbox@v1
        with:
          manifest_path: .agent-sandbox.yml
          llm_api_key: ${{ secrets.LLM_API_KEY }}
          # Optional:
          # llm_api_url: https://api.openai.com/v1/chat/completions
          # llm_model: gpt-4o
          # create_pr: true
          # base_branch: main

      - name: Upload report
        uses: actions/upload-artifact@v4
        with:
          name: agent-sandbox-report
          path: ${{ steps.agent.outputs.report_path }}

      - name: Upload patch
        uses: actions/upload-artifact@v4
        if: ${{ steps.agent.outputs.patch_path != '' }}
        with:
          name: agent-sandbox-patch
          path: ${{ steps.agent.outputs.patch_path }}
```

### Action Inputs

| Input | Required | Default | Description |
|-------|----------|---------|-------------|
| `manifest_path` | No | `.agent-sandbox.yml` | Path to the manifest file. |
| `llm_api_key` | **Yes** | -- | API key for the LLM endpoint. |
| `llm_api_url` | No | OpenAI | Override the LLM API URL. |
| `llm_model` | No | `gpt-4o` | LLM model to use. |
| `create_pr` | No | `false` | Create a draft PR with changes. |
| `base_branch` | No | `main` | Base branch for PR and diff context. |

### Action Outputs

| Output | Description |
|--------|-------------|
| `report_path` | Path to the JSON report file. |
| `patch_path` | Path to the unified diff patch file. |
| `pr_url` | URL of the created draft PR (empty if disabled or no changes). |

### Local CLI

```bash
pip install -e .

# Run against a local repo
agent-sandbox --manifest .agent-sandbox.yml --api-key $LLM_API_KEY --verbose

# Write report and patch to files
agent-sandbox -m .agent-sandbox.yml --output report.json --patch changes.patch

# Diff against a specific branch
agent-sandbox -m .agent-sandbox.yml --base-branch develop --api-key $LLM_API_KEY
```

### Environment Variables

| Variable | Description |
|----------|-------------|
| `AGENT_SANDBOX_API_KEY` | LLM API key (alternative to `--api-key`). |
| `AGENT_SANDBOX_API_URL` | LLM endpoint URL (default: OpenAI). |
| `AGENT_SANDBOX_MODEL` | Model name (default: `gpt-4o`). |

## GitHub Action Setup Guide

### Step 1: Add the manifest

Create `.agent-sandbox.yml` in your repo root. Start with a minimal manifest:

```yaml
allowed_paths:
  read_only:
    - "**/*.py"
  read_write:
    - "src/**"
allowed_commands:
  - "ruff check --fix .*"
network:
  allowed_domains:
    - "api.openai.com"
agent_task:
  description: "Fix lint issues in the source code."
```

### Step 2: Add the API key secret

Go to your repo Settings > Secrets and variables > Actions, and add `LLM_API_KEY` with your OpenAI API key.

### Step 3: Add the workflow

Create `.github/workflows/agent-sandbox.yml` (see the example above).

### Step 4: Test

Open a PR or trigger the workflow manually. Review the uploaded artifacts.

### Enabling Draft PR Creation

To have the action automatically create a draft PR:

1. Set `create_pr: true` in the workflow.
2. Ensure the workflow has `contents: write` and `pull-requests: write` permissions.
3. The `GITHUB_TOKEN` is automatically available in Actions.

## Examples

- `examples/lint-fix/` -- Auto-fix lint issues with ruff
- `examples/changelog/` -- Update CHANGELOG.md from git history

## Troubleshooting

### "Read denied" / "Write denied" errors

Your glob patterns in `allowed_paths` do not match the file the agent is trying to access. Check:
- `**/*.py` matches `.py` files at any depth.
- `*.py` only matches at the root level.
- `src/**` matches anything under `src/`, including deeply nested files.

### "Command not allowed" errors

The command does not match any regex in `allowed_commands`. Remember these are **regex** patterns, not glob patterns. Use `.*` to match any suffix. Test your patterns with `python -c "import re; print(re.fullmatch(r'your pattern', 'your command'))"`.

### "Network access denied" errors

The LLM API domain must be in `network.allowed_domains`. For OpenAI, add `api.openai.com`.

### "LLM request failed" errors

Check your API key and endpoint URL. The agent retries transient failures (429, 5xx) up to 3 times with exponential backoff. If all retries fail, the error is reported.

### Empty patch file

The LLM may not have suggested any file changes. Check the JSON report for the full plan and any errors.

### Docker build failures in CI

Ensure the Dockerfile has access to all required files. The `pyproject.toml` and `src/` directory must be present.

## Development

```bash
# Install with dev dependencies
pip install -e ".[dev]"

# Run tests
pytest

# Run tests with coverage
pytest --cov=src --cov-report=term-missing

# Type checking
mypy src/
```

## Contributing

1. Fork the repository.
2. Create a feature branch: `git checkout -b feature/my-feature`.
3. Make your changes and add tests.
4. Ensure all tests pass: `pytest`.
5. Submit a pull request.

### Code Style

- Follow PEP 8.
- Use type annotations for all function signatures.
- Write docstrings for public classes and functions.
- Add tests for all new functionality.

## License

MIT
