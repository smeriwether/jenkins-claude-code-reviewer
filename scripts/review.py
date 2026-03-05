#!/usr/bin/env python3
"""
Claude Code PR Reviewer — collects diff, calls Claude Code via Bedrock,
posts structured review comments to GitHub.

Usage:
    python3 review.py

Required environment variables:
    GITHUB_TOKEN        - GitHub personal access token (repo scope)
    GITHUB_REPOSITORY   - owner/repo (e.g. smeriwether/my-project)
    PR_NUMBER           - pull request number
    CLAUDE_CODE_USE_BEDROCK=1
    AWS_REGION          - e.g. us-east-1
    AWS_ACCESS_KEY_ID   - (or use instance role)
    AWS_SECRET_ACCESS_KEY

Optional environment variables:
    CLAUDE_MODEL            - Bedrock model ID (default: auto)
    CLAUDE_MAX_TOKENS       - max output tokens (default: 16384)
    INCLUDE_PATTERNS        - comma-separated globs to include (e.g. "*.py,*.js")
    EXCLUDE_PATTERNS        - comma-separated globs to exclude (e.g. "*.lock,*.min.js")
    MAX_DIFF_SIZE           - max diff size in bytes before truncation (default: 100000)
    REVIEW_EVENT            - COMMENT, APPROVE, or REQUEST_CHANGES (default: COMMENT)
    FAIL_ON_FINDINGS        - if "true", exit 1 when issues found (default: false)
"""

import json
import os
import subprocess
import sys
import fnmatch
import re
from urllib.request import Request, urlopen
from urllib.error import HTTPError


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
GITHUB_REPOSITORY = os.environ["GITHUB_REPOSITORY"]
PR_NUMBER = os.environ["PR_NUMBER"]
GITHUB_API = "https://api.github.com"

CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "")
CLAUDE_MAX_TOKENS = os.environ.get("CLAUDE_MAX_TOKENS", "16384")
INCLUDE_PATTERNS = [
    p.strip()
    for p in os.environ.get("INCLUDE_PATTERNS", "").split(",")
    if p.strip()
]
EXCLUDE_PATTERNS = [
    p.strip()
    for p in os.environ.get("EXCLUDE_PATTERNS", "").split(",")
    if p.strip()
]
MAX_DIFF_SIZE = int(os.environ.get("MAX_DIFF_SIZE", "100000"))
REVIEW_EVENT = os.environ.get("REVIEW_EVENT", "COMMENT")
FAIL_ON_FINDINGS = os.environ.get("FAIL_ON_FINDINGS", "false").lower() == "true"


# ---------------------------------------------------------------------------
# JSON Schema for structured Claude output
# ---------------------------------------------------------------------------

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "string",
            "description": "A concise summary of the overall review (2-5 sentences).",
        },
        "comments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative file path from repo root.",
                    },
                    "line": {
                        "type": "integer",
                        "description": "Line number in the new version of the file (RIGHT side of the diff).",
                    },
                    "body": {
                        "type": "string",
                        "description": "The review comment. Use markdown. Include a suggestion if applicable.",
                    },
                    "severity": {
                        "type": "string",
                        "enum": ["critical", "warning", "suggestion", "nitpick"],
                        "description": "Severity of the finding.",
                    },
                },
                "required": ["path", "line", "body", "severity"],
            },
            "description": "Inline comments on specific lines of changed files.",
        },
    },
    "required": ["summary", "comments"],
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def github_api(method: str, path: str, body: dict | None = None) -> dict:
    """Make a GitHub API request."""
    url = f"{GITHUB_API}{path}"
    data = json.dumps(body).encode() if body else None
    req = Request(url, data=data, method=method)
    req.add_header("Authorization", f"token {GITHUB_TOKEN}")
    req.add_header("Accept", "application/vnd.github.v3+json")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urlopen(req) as resp:
            return json.loads(resp.read().decode())
    except HTTPError as e:
        error_body = e.read().decode() if e.fp else ""
        print(f"GitHub API error {e.code}: {error_body}", file=sys.stderr)
        raise


def get_pr_diff() -> str:
    """Fetch the PR diff from GitHub."""
    url = f"{GITHUB_API}/repos/{GITHUB_REPOSITORY}/pulls/{PR_NUMBER}"
    req = Request(url)
    req.add_header("Authorization", f"token {GITHUB_TOKEN}")
    req.add_header("Accept", "application/vnd.github.v3.diff")
    with urlopen(req) as resp:
        return resp.read().decode()


def get_pr_info() -> dict:
    """Fetch PR metadata."""
    return github_api("GET", f"/repos/{GITHUB_REPOSITORY}/pulls/{PR_NUMBER}")


def filter_diff(diff: str) -> str:
    """Filter diff to only include/exclude files matching configured patterns."""
    if not INCLUDE_PATTERNS and not EXCLUDE_PATTERNS:
        return diff

    filtered_sections = []
    current_section = []
    current_file = None

    for line in diff.split("\n"):
        if line.startswith("diff --git"):
            # Save previous section if it passed filters
            if current_file is not None and _file_matches(current_file):
                filtered_sections.append("\n".join(current_section))
            current_section = [line]
            # Extract filename: diff --git a/path b/path
            match = re.search(r"b/(.+)$", line)
            current_file = match.group(1) if match else None
        else:
            current_section.append(line)

    # Don't forget the last section
    if current_file is not None and _file_matches(current_file):
        filtered_sections.append("\n".join(current_section))

    return "\n".join(filtered_sections)


def _file_matches(path: str) -> bool:
    """Check if a file path matches include/exclude patterns."""
    if INCLUDE_PATTERNS:
        if not any(fnmatch.fnmatch(path, p) for p in INCLUDE_PATTERNS):
            return False
    if EXCLUDE_PATTERNS:
        if any(fnmatch.fnmatch(path, p) for p in EXCLUDE_PATTERNS):
            return False
    return True


def parse_diff_line_map(diff: str) -> dict[str, set[int]]:
    """Parse a unified diff and return a map of file -> set of changed line numbers (new side)."""
    file_lines: dict[str, set[int]] = {}
    current_file = None
    current_line = 0

    for line in diff.split("\n"):
        if line.startswith("diff --git"):
            match = re.search(r"b/(.+)$", line)
            current_file = match.group(1) if match else None
            if current_file:
                file_lines.setdefault(current_file, set())
        elif line.startswith("@@"):
            # @@ -old_start,old_count +new_start,new_count @@
            match = re.search(r"\+(\d+)", line)
            current_line = int(match.group(1)) if match else 0
        elif current_file:
            if line.startswith("+") and not line.startswith("+++"):
                file_lines[current_file].add(current_line)
                current_line += 1
            elif line.startswith("-") and not line.startswith("---"):
                pass  # Deleted lines don't increment new-side counter
            else:
                current_line += 1

    return file_lines


def validate_comments(
    comments: list[dict], diff_line_map: dict[str, set[int]]
) -> list[dict]:
    """Filter comments to only include those on valid changed lines."""
    valid = []
    for c in comments:
        path = c.get("path", "")
        line = c.get("line", 0)
        if path in diff_line_map:
            # Accept lines that are in or near changed lines (within 3 lines)
            changed = diff_line_map[path]
            if any(abs(line - cl) <= 3 for cl in changed):
                valid.append(c)
            else:
                print(
                    f"  Skipping comment on {path}:{line} — not near changed lines",
                    file=sys.stderr,
                )
        else:
            print(
                f"  Skipping comment on {path} — file not in diff",
                file=sys.stderr,
            )
    return valid


def run_claude_review(diff: str, pr_info: dict) -> dict:
    """Run Claude Code in print mode with structured output."""
    prompt = f"""You are an expert code reviewer. Review the following pull request diff.

PR Title: {pr_info.get("title", "")}
PR Description: {pr_info.get("body", "") or "No description provided."}

INSTRUCTIONS:
- Focus on: bugs, security issues, performance problems, and code quality.
- Be constructive and specific. Suggest fixes where possible.
- Only comment on lines that are actually changed (added/modified lines).
- Use the exact file paths from the diff (relative to repo root).
- Use the line numbers from the NEW version of the file (right side of diff).
- Severity levels: critical (must fix), warning (should fix), suggestion (consider), nitpick (style/minor).
- Keep the summary concise (2-5 sentences).
- If the code looks good, say so and keep comments minimal.
- Do NOT fabricate issues. Only report real problems you can identify.

DIFF:
```
{diff}
```"""

    cmd = ["claude", "-p", prompt, "--output-format", "json"]
    cmd += ["--json-schema", json.dumps(REVIEW_SCHEMA)]

    if CLAUDE_MODEL:
        cmd += ["--model", CLAUDE_MODEL]

    env = os.environ.copy()
    env["CLAUDE_CODE_USE_BEDROCK"] = "1"

    print("Running Claude Code review...", file=sys.stderr)
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=env,
        timeout=300,
    )

    if result.returncode != 0:
        print(f"Claude Code stderr: {result.stderr}", file=sys.stderr)
        raise RuntimeError(f"Claude Code exited with code {result.returncode}")

    response = json.loads(result.stdout)

    # Claude --output-format json wraps the result
    if "structured_output" in response:
        return response["structured_output"]
    elif "result" in response:
        # result may be a JSON string
        r = response["result"]
        if isinstance(r, str):
            try:
                return json.loads(r)
            except json.JSONDecodeError:
                return {"summary": r, "comments": []}
        return r
    else:
        return response


def post_review(pr_info: dict, review: dict, diff_line_map: dict) -> None:
    """Post the review to GitHub as a PR review with inline comments."""
    summary = review.get("summary", "No summary provided.")
    raw_comments = review.get("comments", [])

    # Validate comments against actual diff
    comments = validate_comments(raw_comments, diff_line_map)

    skipped = len(raw_comments) - len(comments)
    if skipped:
        print(f"  {skipped} comments skipped (not on changed lines)", file=sys.stderr)

    # Format severity badges
    severity_icons = {
        "critical": "\u2757",
        "warning": "\u26a0\ufe0f",
        "suggestion": "\U0001f4a1",
        "nitpick": "\U0001f9f9",
    }

    # Build review body
    body_parts = [
        "## Claude Code Review\n",
        summary,
        "",
    ]

    if comments:
        counts = {}
        for c in comments:
            sev = c.get("severity", "suggestion")
            counts[sev] = counts.get(sev, 0) + 1
        stats = " | ".join(
            f"{severity_icons.get(s, '')} {s}: {n}" for s, n in sorted(counts.items())
        )
        body_parts.append(f"\n**Findings:** {stats}")
    else:
        body_parts.append("\nNo inline findings.")

    body_parts.append(
        "\n---\n*Automated review by [Claude Code](https://code.claude.com) via Jenkins*"
    )

    review_body = "\n".join(body_parts)

    # Format inline comments for GitHub API
    gh_comments = []
    for c in comments:
        sev = c.get("severity", "suggestion")
        icon = severity_icons.get(sev, "")
        gh_comments.append(
            {
                "path": c["path"],
                "line": c["line"],
                "side": "RIGHT",
                "body": f"**{icon} {sev}**: {c['body']}",
            }
        )

    # Get the head SHA for the review
    head_sha = pr_info.get("head", {}).get("sha", "")

    payload: dict = {
        "body": review_body,
        "event": REVIEW_EVENT,
    }
    if head_sha:
        payload["commit_id"] = head_sha
    if gh_comments:
        payload["comments"] = gh_comments

    print(f"Posting review with {len(gh_comments)} inline comments...", file=sys.stderr)
    github_api(
        "POST",
        f"/repos/{GITHUB_REPOSITORY}/pulls/{PR_NUMBER}/reviews",
        payload,
    )
    print("Review posted successfully.", file=sys.stderr)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    print(f"Reviewing PR #{PR_NUMBER} in {GITHUB_REPOSITORY}", file=sys.stderr)

    # 1. Fetch PR info and diff
    pr_info = get_pr_info()
    diff = get_pr_diff()
    print(f"  Diff size: {len(diff)} bytes", file=sys.stderr)

    # 2. Filter diff
    diff = filter_diff(diff)
    if not diff.strip():
        print("  No files match filters — skipping review.", file=sys.stderr)
        return 0

    # 3. Truncate if too large
    if len(diff) > MAX_DIFF_SIZE:
        print(
            f"  Diff exceeds {MAX_DIFF_SIZE} bytes — truncating.",
            file=sys.stderr,
        )
        diff = diff[:MAX_DIFF_SIZE] + "\n... [truncated]"

    # 4. Parse diff for line validation
    diff_line_map = parse_diff_line_map(diff)
    changed_files = len(diff_line_map)
    changed_lines = sum(len(v) for v in diff_line_map.values())
    print(
        f"  {changed_files} files changed, {changed_lines} lines added",
        file=sys.stderr,
    )

    # 5. Run Claude Code review
    review = run_claude_review(diff, pr_info)

    # 6. Post review to GitHub
    post_review(pr_info, review, diff_line_map)

    # 7. Optionally fail the build
    findings = len(review.get("comments", []))
    critical = sum(
        1 for c in review.get("comments", []) if c.get("severity") == "critical"
    )

    if FAIL_ON_FINDINGS and critical > 0:
        print(
            f"Failing build: {critical} critical finding(s).",
            file=sys.stderr,
        )
        return 1

    print(f"Review complete: {findings} finding(s).", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
