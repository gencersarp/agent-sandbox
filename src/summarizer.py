import json
import os
import sys
from pathlib import Path

def generate_summary(report_path: str, output_path: str):
    try:
        with open(report_path, 'r') as f:
            report = json.load(f)
    except Exception as e:
        print(f"Error loading report: {e}")
        return

    summary = []
    summary.append("### 🤖 Agent Sandbox Summary")
    
    status = "✅ Success" if not report.get("errors") else "⚠️ Completed with Errors"
    summary.append(f"**Status:** {status}")
    
    summary.append(f"\n#### 📝 Summary\n{report.get('summary', 'No summary provided.')}")

    if report.get("files_modified"):
        summary.append("\n#### 📂 Files Modified")
        for file in report["files_modified"]:
            summary.append(f"- `{file}`")

    if report.get("commands_executed"):
        summary.append("\n#### 🐚 Commands Executed")
        for cmd in report["commands_executed"]:
            icon = "✅" if cmd.get("exit_code") == 0 else "❌"
            summary.append(f"- {icon} `{cmd.get('command')}` (exit: {cmd.get('exit_code')})")

    if report.get("fetches"):
        summary.append("\n#### 🌐 Network Fetches")
        for fetch in report["fetches"]:
            status_icon = "✅" if 200 <= fetch.get("status_code", 0) < 300 else "❌"
            summary.append(f"- {status_icon} `{fetch.get('url')}` (status: {fetch.get('status_code')})")

    if report.get("errors"):
        summary.append("\n#### ❌ Errors")
        for err in report["errors"]:
            summary.append(f"- {err}")

    if report.get("comments"):
        summary.append("\n#### 💬 Comments")
        for comment in report["comments"]:
            summary.append(f"- **`{comment.get('path')}:{comment.get('line')}`**: {comment.get('text')}")

    if report.get("unified_diff"):
        summary.append("\n<details><summary><b>🔍 View Changes (Diff)</b></summary>\n")
        summary.append("```diff")
        summary.append(report["unified_diff"])
        summary.append("```")
        summary.append("\n</details>")

    with open(output_path, 'a') as f:
        f.write("\n".join(summary) + "\n")

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python summarizer.py <report_path> <summary_output_path>")
        sys.exit(1)
    generate_summary(sys.argv[1], sys.argv[2])
