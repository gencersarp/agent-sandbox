# Progress Tracker

## Last Session Summary
- **Date:** Sunday, May 10, 2026
- **Status:** Implemented multi-turn execution, granular git control, and enhanced reporting.
- **Accomplishments:**
    - Implemented multi-turn agent execution loop in `AgentRunner`, allowing the agent to react to results of its actions (read, run, git, list_dir, fetch).
    - Added `GitPolicy` to manifest with `allowed_subcommands` support for granular git control.
    - Updated `SandboxedCommandRunner` to enforce git subcommand restrictions via `run_git`.
    - Enhanced `AgentReport` and `summarizer.py` to include details from `list_dir` and specialized `git` command logs.
    - Improved sandbox violation error messages to be more descriptive, including matching patterns and "Ignored" status.
    - Updated and verified all tests (`test_manifest.py`, `test_sandbox.py`, `test_agent.py`, `test_new_features.py`, `test_integration.py`).
    - Refined the agent system prompt to support multi-turn conversation and provide a clear "done" signal.

## Next Session TODO
- Implement support for `allowed_domains` with wildcard support (e.g., `*.github.com`) in the `NetworkGuard` (partially done, but could be more robust).
- Add resource usage tracking (CPU time, memory) for the sandboxed process.
- Implement a "dry run" mode for the GitHub Action that only shows proposed changes without generating patches.
- Create a more comprehensive integration test that simulates a full multi-turn agent interaction.

## Architecture Decisions
- **Multi-turn Loop Robustness:** Decided to break the multi-turn loop on non-JSON responses after the first turn. This handles cases where the LLM provides a natural language conclusion and ensures backward compatibility with single-turn mocks in tests.
- **Git Action Enforcement:** Chose to have a dedicated `run_git` method in `SandboxedCommandRunner` to separately enforce git subcommands from general shell commands.
- **Feedback-driven turns:** The agent now receives structured JSON feedback of its previous actions in each subsequent turn of the conversation.

## Known Issues
- The current multi-turn loop is limited to a fixed `max_turns` (default 5). This should eventually be configurable via the manifest.
- Large command outputs or file contents in multi-turn feedback might consume significant token budget.
