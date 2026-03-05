#!/usr/bin/env python3
"""
Claude Code MR Reviewer — collects diff, calls Claude Code via Bedrock,
posts structured review comments to GitLab merge requests.

Usage:
    python3 review.py

Required environment variables:
    GITLAB_TOKEN        - GitLab personal access token (api scope)
    GITLAB_PROJECT_ID   - GitLab project ID (numeric) or URL-encoded path
    MR_IID              - Merge request IID (internal ID)
    CLAUDE_CODE_USE_BEDROCK=1
    AWS_REGION          - e.g. us-east-1
    AWS_ACCESS_KEY_ID   - (or use instance role)
    AWS_SECRET_ACCESS_KEY

Optional environment variables:
    GITLAB_API_URL          - GitLab API base URL (default: https://gitlab.com/api/v4)
    CLAUDE_MODEL            - Bedrock model ID (default: auto)
    CLAUDE_MAX_TOKENS       - max output tokens (default: 16384)
    INCLUDE_PATTERNS        - comma-separated globs to include (e.g. "*.py,*.js")
    EXCLUDE_PATTERNS        - comma-separated globs to exclude (e.g. "*.lock,*.min.js")
    MAX_DIFF_SIZE           - max diff size in bytes before truncation (default: 100000)
    FAIL_ON_FINDINGS        - if "true", exit 1 when critical issues found (default: false)
"""

import json
import os
import subprocess
import sys
import fnmatch
import re
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from urllib.parse import quote as urlquote


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

GITLAB_TOKEN = os.environ["GITLAB_TOKEN"]
GITLAB_PROJECT_ID = os.environ["GITLAB_PROJECT_ID"]
MR_IID = os.environ["MR_IID"]
GITLAB_API_URL = os.environ.get("GITLAB_API_URL", "https://gitlab.com/api/v4")

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
FAIL_ON_FINDINGS = os.environ.get("FAIL_ON_FINDINGS", "false").lower() == "true"

# URL-encode the project ID if it contains slashes (path-based ID)
PROJECT_ID_ENCODED = urlquote(GITLAB_PROJECT_ID, safe="")


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


def gitlab_api(method: str, path: str, body: dict | None = None) -> dict | list:
    """Make a GitLab API request."""
    url = f"{GITLAB_API_URL}{path}"
    data = json.dumps(body).encode() if body else None
    req = Request(url, data=data, method=method)
    req.add_header("PRIVATE-TOKEN", GITLAB_TOKEN)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urlopen(req) as resp:
            return json.loads(resp.read().decode())
    except HTTPError as e:
        error_body = e.read().decode() if e.fp else ""
        print(f"GitLab API error {e.code}: {error_body}", file=sys.stderr)
        raise


def get_mr_info() -> dict:
    """Fetch MR metadata including diff_refs."""
    return gitlab_api(
        "GET",
        f"/projects/{PROJECT_ID_ENCODED}/merge_requests/{MR_IID}",
    )


def get_mr_changes() -> list[dict]:
    """Fetch MR file changes with diffs."""
    data = gitlab_api(
        "GET",
        f"/projects/{PROJECT_ID_ENCODED}/merge_requests/{MR_IID}/changes",
    )
    return data.get("changes", [])


def build_unified_diff(changes: list[dict]) -> str:
    """Reconstruct a unified diff string from GitLab MR changes."""
    parts = []
    for change in changes:
        old_path = change.get("old_path", "")
        new_path = change.get("new_path", "")
        diff_text = change.get("diff", "")
        if not diff_text:
            continue
        # Add a standard diff --git header
        parts.append(f"diff --git a/{old_path} b/{new_path}")
        parts.append(diff_text.rstrip("\n"))
    return "\n".join(parts)


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


def run_claude_review(diff: str, mr_info: dict) -> dict:
    """Run Claude Code in print mode with structured output."""
    prompt = f"""You are an expert code reviewer. Review the following merge request diff.

MR Title: {mr_info.get("title", "")}
MR Description: {mr_info.get("description", "") or "No description provided."}

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


def post_review(mr_info: dict, review: dict, diff_line_map: dict) -> None:
    """Post the review to GitLab as MR notes and inline discussions."""
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

    # Build summary note body
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

    summary_body = "\n".join(body_parts)

    # Post summary as an MR note
    print("Posting summary note...", file=sys.stderr)
    gitlab_api(
        "POST",
        f"/projects/{PROJECT_ID_ENCODED}/merge_requests/{MR_IID}/notes",
        {"body": summary_body},
    )

    # Post inline comments as MR discussions with position info
    diff_refs = mr_info.get("diff_refs", {})
    base_sha = diff_refs.get("base_sha", "")
    head_sha = diff_refs.get("head_sha", "")
    start_sha = diff_refs.get("start_sha", "")

    if not (base_sha and head_sha and start_sha):
        print(
            "  Warning: diff_refs missing from MR info — skipping inline comments.",
            file=sys.stderr,
        )
        if comments:
            # Fall back: post all inline comments as a single note
            _post_comments_as_note(comments, severity_icons)
        return

    posted = 0
    failed_comments = []
    for c in comments:
        sev = c.get("severity", "suggestion")
        icon = severity_icons.get(sev, "")
        comment_body = f"**{icon} {sev}**: {c['body']}"

        position = {
            "base_sha": base_sha,
            "head_sha": head_sha,
            "start_sha": start_sha,
            "position_type": "text",
            "new_path": c["path"],
            "old_path": c["path"],
            "new_line": c["line"],
        }

        try:
            gitlab_api(
                "POST",
                f"/projects/{PROJECT_ID_ENCODED}/merge_requests/{MR_IID}/discussions",
                {"body": comment_body, "position": position},
            )
            posted += 1
        except HTTPError:
            failed_comments.append(c)
            print(
                f"  Failed to post inline comment on {c['path']}:{c['line']} — "
                "will include in fallback note.",
                file=sys.stderr,
            )

    print(
        f"  Inline comments: {posted} posted, {len(failed_comments)} failed.",
        file=sys.stderr,
    )

    # If any inline comments failed, post them as a fallback note
    if failed_comments:
        _post_comments_as_note(failed_comments, severity_icons)

    print("Review posted successfully.", file=sys.stderr)


def _post_comments_as_note(comments: list[dict], severity_icons: dict) -> None:
    """Post inline comments as a single MR note (fallback when discussions fail)."""
    lines = ["### Inline Comments (fallback)\n"]
    for c in comments:
        sev = c.get("severity", "suggestion")
        icon = severity_icons.get(sev, "")
        lines.append(f"**{icon} {sev}** — `{c['path']}:{c['line']}`\n{c['body']}\n")

    gitlab_api(
        "POST",
        f"/projects/{PROJECT_ID_ENCODED}/merge_requests/{MR_IID}/notes",
        {"body": "\n".join(lines)},
    )
    print("  Fallback note posted with inline comments.", file=sys.stderr)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    print(f"Reviewing MR !{MR_IID} in project {GITLAB_PROJECT_ID}", file=sys.stderr)

    # 1. Fetch MR info and changes
    mr_info = get_mr_info()
    changes = get_mr_changes()
    print(f"  {len(changes)} file(s) changed in MR", file=sys.stderr)

    # 2. Build unified diff from changes
    diff = build_unified_diff(changes)
    print(f"  Diff size: {len(diff)} bytes", file=sys.stderr)

    # 3. Filter diff
    diff = filter_diff(diff)
    if not diff.strip():
        print("  No files match filters — skipping review.", file=sys.stderr)
        return 0

    # 4. Truncate if too large
    if len(diff) > MAX_DIFF_SIZE:
        print(
            f"  Diff exceeds {MAX_DIFF_SIZE} bytes — truncating.",
            file=sys.stderr,
        )
        diff = diff[:MAX_DIFF_SIZE] + "\n... [truncated]"

    # 5. Parse diff for line validation
    diff_line_map = parse_diff_line_map(diff)
    changed_files = len(diff_line_map)
    changed_lines = sum(len(v) for v in diff_line_map.values())
    print(
        f"  {changed_files} files changed, {changed_lines} lines added",
        file=sys.stderr,
    )

    # 6. Run Claude Code review
    review = run_claude_review(diff, mr_info)

    # 7. Post review to GitLab
    post_review(mr_info, review, diff_line_map)

    # 8. Optionally fail the build
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
