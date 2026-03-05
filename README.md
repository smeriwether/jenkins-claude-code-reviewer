# Jenkins Claude Code Reviewer

A reusable Jenkins pipeline that runs [Claude Code](https://code.claude.com) as an automated code reviewer on GitHub pull requests, powered by **AWS Bedrock**.

Drop it into any repo. Claude reviews the diff, posts a summary comment and inline findings directly on the PR.

## How It Works

1. A pull request triggers the Jenkins pipeline
2. The pipeline installs Claude Code in a Docker container
3. The PR diff is fetched from GitHub and filtered by file patterns
4. Claude Code analyzes the diff via AWS Bedrock (no Anthropic API key needed)
5. Structured review output (summary + inline comments with severity) is posted back to the PR as a GitHub review

## Prerequisites

- **Jenkins** with Pipeline and Docker Pipeline plugins
- **Docker** available on the Jenkins agent
- **AWS account** with Bedrock access and Claude models enabled
- **GitHub personal access token** with `repo` scope
- **Node.js 18+** available in the Docker image (default: `node:20-slim`)

## Quick Start

### Option A: Jenkins Shared Library (recommended)

1. **Add the Shared Library** in Jenkins:
   - Go to *Manage Jenkins → System → Global Pipeline Libraries*
   - Add a library:
     - **Name:** `claude-code-reviewer`
     - **Default version:** `main`
     - **Retrieval method:** Modern SCM → Git
     - **Project Repository:** `https://github.com/smeriwether/jenkins-claude-code-reviewer.git`

2. **Create Jenkins credentials:**
   - `github-token` → *Secret text* — your GitHub PAT
   - `aws-bedrock-creds` → *Username with password* — AWS Access Key ID / Secret Access Key

3. **Add to your repo's Jenkinsfile:**

```groovy
@Library('claude-code-reviewer') _

pipeline {
    agent any
    stages {
        stage('Claude Code Review') {
            when { expression { env.CHANGE_ID != null } }
            steps {
                claudeReview(
                    awsCredentialsId: 'aws-bedrock-creds',
                    githubTokenCredentialsId: 'github-token',
                    awsRegion: 'us-east-1',
                )
            }
        }
    }
}
```

### Option B: Standalone Jenkinsfile

Copy `scripts/review.py` and `examples/Jenkinsfile.standalone` into your repo. No Shared Library setup required. See the [standalone example](examples/Jenkinsfile.standalone) for details.

## Configuration

All parameters are passed to `claudeReview()` (Shared Library) or set as environment variables (standalone).

| Parameter | Env Var | Default | Description |
|-----------|---------|---------|-------------|
| `awsCredentialsId` | — | `aws-bedrock-creds` | Jenkins credentials ID for AWS |
| `githubTokenCredentialsId` | — | `github-token` | Jenkins credentials ID for GitHub token |
| `awsRegion` | `AWS_REGION` | `us-east-1` | AWS region for Bedrock |
| `claudeModel` | `CLAUDE_MODEL` | *(auto)* | Bedrock model ID (e.g. `us.anthropic.claude-sonnet-4-6`) |
| `maxTokens` | `CLAUDE_MAX_TOKENS` | `16384` | Max output tokens |
| `includePatterns` | `INCLUDE_PATTERNS` | *(all files)* | Comma-separated globs to include (e.g. `*.py,*.js`) |
| `excludePatterns` | `EXCLUDE_PATTERNS` | *(none)* | Comma-separated globs to exclude (e.g. `*.lock,*.min.js`) |
| `maxDiffSize` | `MAX_DIFF_SIZE` | `100000` | Max diff size in bytes before truncation |
| `reviewEvent` | `REVIEW_EVENT` | `COMMENT` | GitHub review event: `COMMENT`, `APPROVE`, or `REQUEST_CHANGES` |
| `failOnFindings` | `FAIL_ON_FINDINGS` | `false` | Fail the build when critical findings are found |
| `dockerImage` | — | `node:20-slim` | Docker image for the review container |
| `repository` | `GITHUB_REPOSITORY` | *(auto-detected)* | `owner/repo` override |
| `prNumber` | `PR_NUMBER` | *(auto-detected)* | PR number override |

### Model Pinning

For production stability, pin your Bedrock model to avoid breakage when new models are released:

```groovy
claudeReview(
    claudeModel: 'us.anthropic.claude-sonnet-4-6',
    // ...
)
```

Or set environment variables in Jenkins:
```
ANTHROPIC_DEFAULT_SONNET_MODEL=us.anthropic.claude-sonnet-4-6
```

## AWS IAM Policy

The AWS credentials need the following Bedrock permissions:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "bedrock:InvokeModel",
        "bedrock:InvokeModelWithResponseStream",
        "bedrock:ListInferenceProfiles"
      ],
      "Resource": [
        "arn:aws:bedrock:*:*:inference-profile/*",
        "arn:aws:bedrock:*:*:application-inference-profile/*",
        "arn:aws:bedrock:*:*:foundation-model/*"
      ]
    }
  ]
}
```

You must also **enable Claude models** in the AWS Bedrock console (Model access → Request access).

## Project Structure

```
├── vars/
│   └── claudeReview.groovy       # Jenkins Shared Library step
├── resources/
│   └── scripts/
│       └── review.py             # Review script (loaded by shared library)
├── scripts/
│   └── review.py                 # Review script (standalone use)
├── examples/
│   ├── Jenkinsfile               # Shared Library usage example
│   └── Jenkinsfile.standalone    # Standalone usage example
└── README.md
```

## How the Review Script Works

1. **Fetches the PR diff** from GitHub API (`Accept: application/vnd.github.v3.diff`)
2. **Filters files** by include/exclude glob patterns
3. **Truncates** large diffs to stay within model context limits
4. **Calls Claude Code** in non-interactive mode (`claude -p`) with:
   - Structured JSON output (`--output-format json --json-schema ...`)
   - A review prompt including PR title, description, and diff
5. **Validates** inline comment line numbers against the actual diff (skips comments on unchanged lines)
6. **Posts a GitHub review** via `POST /repos/{owner}/{repo}/pulls/{number}/reviews` with:
   - Summary body with severity breakdown
   - Inline comments on specific changed lines

### Severity Levels

| Level | Icon | Meaning |
|-------|------|---------|
| `critical` | ❗ | Must fix — bugs, security vulnerabilities |
| `warning` | ⚠️ | Should fix — potential issues, bad patterns |
| `suggestion` | 💡 | Consider — improvements, alternatives |
| `nitpick` | 🧹 | Minor — style, naming, formatting |

## Troubleshooting

### "No PR number detected"
The pipeline relies on `CHANGE_ID` (set by the GitHub Branch Source plugin) or `ghprbPullId` (set by the GitHub Pull Request Builder plugin). Ensure your Jenkins job is configured as a multibranch pipeline with the GitHub source.

### "Claude Code exited with code 1"
- Check that `CLAUDE_CODE_USE_BEDROCK=1` is set
- Verify AWS credentials are valid and have Bedrock permissions
- Ensure Claude models are enabled in your Bedrock region
- Check the Jenkins console output for Claude Code's stderr

### "GitHub API error 422"
This usually means an inline comment references a line that isn't part of the PR diff. The script validates comments against the diff, but edge cases can occur. The review summary will still be posted even if some inline comments are rejected.

### "GitHub API error 403"
The GitHub token doesn't have sufficient permissions. Ensure it has the `repo` scope (or `public_repo` for public repos only).

### Large PRs are slow or truncated
Increase `maxDiffSize` or use `includePatterns` / `excludePatterns` to focus the review on relevant files. Very large diffs may hit Bedrock's context limits.

### Docker image issues
The default `node:20-slim` image includes Node.js and Python 3. If your Jenkins agent doesn't have Docker, set `dockerImage` to a custom image or use the standalone Jenkinsfile without Docker.

## References

- [Claude Code Documentation](https://code.claude.com/docs)
- [Claude Code CLI Reference](https://code.claude.com/docs/en/cli-reference)
- [Claude Code Headless / CI Mode](https://code.claude.com/docs/en/headless)
- [Claude Code + Amazon Bedrock](https://code.claude.com/docs/en/amazon-bedrock)
- [claude-code-action](https://github.com/anthropics/claude-code-action) — Anthropic's official GitHub Action for Claude Code reviews
- [claude-code-security-review](https://github.com/anthropics/claude-code-security-review) — Security-focused variant
- [CodeRabbit ai-pr-reviewer](https://github.com/coderabbitai/ai-pr-reviewer) — AI code review action (reference for diff parsing strategy)
- [Qodo PR-Agent](https://github.com/qodo-ai/pr-agent) — AI code review tool (reference for prompt design)
- [GitHub API: Pull Request Reviews](https://docs.github.com/en/rest/pulls/reviews)
- [AWS Bedrock Claude Models](https://docs.aws.amazon.com/bedrock/latest/userguide/models-supported.html)

## License

MIT
