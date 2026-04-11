# Progress Tracker

## Last Session Summary
- **Date:** Saturday, April 11, 2026
- **Status:** Enhanced agent capabilities and improved reporting.
- **Accomplishments:**
    - Implemented `fetch` (network) action in `AgentRunner` with `NetworkGuard` enforcement.
    - Added `fetches` tracking to `AgentReport` and updated JSON serialization.
    - Created `src/summarizer.py` to generate markdown summaries for `GITHUB_STEP_SUMMARY`.
    - Updated `entrypoint.sh` to use the new summarizer, improving job log visibility.
    - Enhanced `src/pr_creator.py` to support line-level PR comments using the GitHub Reviews API.
    - Added integration tests for `fetch` and `comment` actions.
    - Verified all 85 tests are passing.

## Next Session TODO
- Add support for file-level "meta" instructions in the manifest (e.g., "always ignore node_modules").
- Implement a more robust way to handle large repo file lists (currently truncated to 200).
- Explore adding `git` action for more advanced repo manipulation within the sandbox.
- Add more examples to the `examples/` directory demonstrating new capabilities.

## Architecture Decisions
- **Python-based Runner:** Chose Python for its rich ecosystem of LLM and developer tools.
- **Pydantic for Manifests:** Using Pydantic for robust YAML schema validation and clear error reporting.
- **Application-Layer Sandboxing:** Enforcing policies (path globs, command regexes, domain allowlists) at the application layer for flexibility and clear feedback, while acknowledging OS-level limitations.
- **Draft PR Workflow:** Preferring draft PRs for human-in-the-loop review over direct commits.
- **Line-level Comments via Review API:** Using the GitHub Reviews API to bulk-add agent comments to PRs, providing a better review experience.

## Known Issues
- Network enforcement for shell commands (`run` action) depends solely on command regexes and is not integrated with `NetworkGuard`.
- LLM response parsing may still be fragile for very complex nested structures (partially addressed by code fence stripping).
- Line-level comments currently assume the `line` provided by the agent refers to the current version of the file (RIGHT side in GitHub PR).
