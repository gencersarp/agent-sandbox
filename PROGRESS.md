# Progress Tracker

## Last Session Summary
- **Date:** Wednesday, April 8, 2026
- **Status:** Initialized project with full core functionality.
- **Accomplishments:**
    - Established project structure and Python environment (`pyproject.toml`).
    - Implemented manifest parsing and validation (`src/manifest.py`) with Pydantic.
    - Built the sandbox layer with filesystem, command, and network enforcement (`src/sandbox.py`).
    - Implemented the core agent runner with LLM orchestration and unified diff generation (`src/agent.py`).
    - Created the CLI interface (`src/cli.py`) and GitHub Action integration (`action.yml`, `Dockerfile`, `entrypoint.sh`).
    - Added draft PR creation capability (`src/pr_creator.py`).
    - Added `comment` action to the agent runner and report for line-level feedback.
    - Fixed a failing test in `tests/test_agent.py` related to nested code fences in LLM responses.
    - Verified all 82 tests are passing.

## Next Session TODO
- Enhance agent capabilities with a `fetch` (network) action.
- Improve GitHub Action reporting by adding step summaries to the job log.
- Add more exhaustive integration tests for the full CLI-to-PR flow.
- Explore supporting line-level PR comments in `pr_creator.py`.

## Architecture Decisions
- **Python-based Runner:** Chose Python for its rich ecosystem of LLM and developer tools.
- **Pydantic for Manifests:** Using Pydantic for robust YAML schema validation and clear error reporting.
- **Application-Layer Sandboxing:** Enforcing policies (path globs, command regexes, domain allowlists) at the application layer for flexibility and clear feedback, while acknowledging OS-level limitations.
- **Draft PR Workflow:** Preferring draft PRs for human-in-the-loop review over direct commits.

## Known Issues
- Network enforcement for shell commands (`run` action) depends solely on command regexes and is not integrated with `NetworkGuard`.
- LLM response parsing may still be fragile for very complex nested structures (partially addressed by code fence stripping).
