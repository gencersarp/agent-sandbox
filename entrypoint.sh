#!/usr/bin/env bash
set -uo pipefail
# Note: we do NOT set -e because we want to capture the agent's exit code
# and still upload artifacts even if the agent fails.

MANIFEST="${INPUT_MANIFEST_PATH:-.agent-sandbox.yml}"
REPO_ROOT="${GITHUB_WORKSPACE:-.}"
OUTPUT_DIR="${RUNNER_TEMP:-/tmp}/agent-sandbox-output"
CREATE_PR="${INPUT_CREATE_PR:-false}"
BASE_BRANCH="${INPUT_BASE_BRANCH:-main}"

mkdir -p "$OUTPUT_DIR"

REPORT_FILE="$OUTPUT_DIR/report.json"
PATCH_FILE="$OUTPUT_DIR/changes.patch"

echo "::group::Agent Sandbox - Configuration"
echo "Manifest:    $MANIFEST"
echo "Repo root:   $REPO_ROOT"
echo "Output dir:  $OUTPUT_DIR"
echo "Create PR:   $CREATE_PR"
echo "Base branch: $BASE_BRANCH"
echo "::endgroup::"

# ------------------------------------------------------------------
# Run the agent
# ------------------------------------------------------------------
echo "::group::Agent Sandbox - Execution"
python -m src \
  --manifest "$MANIFEST" \
  --repo-root "$REPO_ROOT" \
  --api-key "${INPUT_LLM_API_KEY}" \
  --base-branch "$BASE_BRANCH" \
  --output "$REPORT_FILE" \
  --patch "$PATCH_FILE" \
  --verbose

AGENT_EXIT=$?
echo "::endgroup::"

if [ $AGENT_EXIT -ne 0 ]; then
    echo "::warning::Agent exited with code $AGENT_EXIT"
fi

# ------------------------------------------------------------------
# Validate outputs
# ------------------------------------------------------------------
if [ ! -f "$REPORT_FILE" ]; then
    echo "::warning::Report file not found; creating minimal report."
    cat > "$REPORT_FILE" <<'FALLBACK'
{"files_modified":[],"commands_executed":[],"errors":["Agent did not produce a report"],"summary":"Agent failed before producing a report.","unified_diff":""}
FALLBACK
fi

# Validate report is valid JSON
if ! python -c "import json, sys; json.load(open(sys.argv[1]))" "$REPORT_FILE" 2>/dev/null; then
    echo "::error::Report file is not valid JSON."
    cat > "$REPORT_FILE" <<'FALLBACK'
{"files_modified":[],"commands_executed":[],"errors":["Report was not valid JSON"],"summary":"Agent produced an invalid report.","unified_diff":""}
FALLBACK
fi

# ------------------------------------------------------------------
# Generate step summary
# ------------------------------------------------------------------
if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
    echo "::group::Agent Sandbox - Generating Step Summary"
    python src/summarizer.py "$REPORT_FILE" "$GITHUB_STEP_SUMMARY"
    echo "::endgroup::"
fi

# ------------------------------------------------------------------
# Set outputs for downstream steps
# ------------------------------------------------------------------
echo "report_path=$REPORT_FILE" >> "$GITHUB_OUTPUT"
echo "patch_path=$PATCH_FILE" >> "$GITHUB_OUTPUT"

# ------------------------------------------------------------------
# Optional: create a draft PR
# ------------------------------------------------------------------
PR_URL=""
if [ "$CREATE_PR" = "true" ] && [ -f "$PATCH_FILE" ] && [ -s "$PATCH_FILE" ]; then
    echo "::group::Agent Sandbox - Creating Draft PR"

    PR_URL=$(python -c "
import json, os, sys
sys.path.insert(0, '.')
from src.pr_creator import create_pr

report_path = '$REPORT_FILE'
try:
    with open(report_path) as f:
        report = json.load(f)
    task_desc = report.get('summary', 'Automated changes')[:72]
except Exception:
    task_desc = 'Automated changes'

url = create_pr(
    repo_root='$REPO_ROOT',
    patch_path='$PATCH_FILE',
    report_path=report_path,
    task_description=task_desc,
    github_token=os.environ.get('GITHUB_TOKEN', ''),
    base_branch='$BASE_BRANCH',
)
print(url or '')
" 2>&1)

    if [ -n "$PR_URL" ] && [ "$PR_URL" != "None" ] && [ "$PR_URL" != "" ]; then
        echo "::notice::Draft PR created: $PR_URL"
    else
        echo "::warning::Failed to create draft PR."
        PR_URL=""
    fi

    echo "::endgroup::"
fi

echo "pr_url=$PR_URL" >> "$GITHUB_OUTPUT"

# ------------------------------------------------------------------
# Exit with original agent exit code
# ------------------------------------------------------------------
exit $AGENT_EXIT
