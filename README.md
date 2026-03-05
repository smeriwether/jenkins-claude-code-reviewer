# Jenkins Claude Code Reviewer (GitHub)

A reusable Jenkins pipeline that runs [Claude Code](https://code.claude.com) as an automated code reviewer on **GitHub pull requests**, powered by **AWS Bedrock**.

Drop it into any repo. Claude reviews the PR diff, posts a review with a summary and inline comments directly on the pull request.

## How It Works

1. A pull request triggers the Jenkins pipeline
2. The pipeline installs Claude Code in a Docker container
3. The PR diff is fetched from the GitHub API and filtered by file patterns
4. Claude Code analyzes the diff via AWS Bedrock (no Anthropic API key needed)
5. A PR review is posted with a summary and inline comments on changed lines

## Prerequisites

- **Jenkins** with Pipeline and Docker Pipeline plugins
- **Docker** available on the Jenkins agent
- **AWS account** with Bedrock access and Claude models enabled
- **GitHub personal access token** with appropriate permissions (see below)

> AWS auth: this project is designed for **role-based AWS credentials** (instance profile / IRSA / assumed role). It does **not** require static access keys.

## Quick Start

### Option A: Jenkins Shared Library (recommended)

1. **Add the Shared Library** in Jenkins:
   - Go to *Manage Jenkins -> System -> Global Pipeline Libraries*
   - Add a library:
     - **Name:** `claude-code-reviewer`
     - **Default version:** `main`
     - **Retrieval method:** Modern SCM -> Git
     - **Project Repository:** `https://github.com/smeriwether/jenkins-claude-code-reviewer.git`

2. **Create Jenkins credentials:**
   - `github-token` -> *Secret text* -- your GitHub PAT (see token requirements below)

   AWS credentials are **not** configured as Jenkins secrets. Use role-based auth on the Jenkins agent (instance profile / IRSA / assumed role).

3. **Add to your repo's Jenkinsfile:**

```groovy
@Library('claude-code-reviewer') _

pipeline {
    agent any
    stages {
        stage('Claude Code Review') {
            when {
                expression {
                    env.CHANGE_ID != null ||
                    env.ghprbPullId != null
                }
            }
            steps {
                claudeReview(
                    githubTokenCredentialsId: 'github-token',
                    awsRegion: 'us-east-1',
                    bedrockInferenceProfile: 'arn:aws:bedrock:us-east-1:123456789012:inference-profile/your-profile',
                )
            }
        }
    }
}
```

### Option B: Standalone Jenkinsfile

Copy `scripts/review.py` and `examples/Jenkinsfile.standalone` into your repo. No Shared Library setup required. See the [standalone example](examples/Jenkinsfile.standalone) for details.

## GitHub Authentication

The review script authenticates to GitHub using a **Personal Access Token (PAT)**.

| Token type | Required permissions | Notes |
|---|---|---|
| Fine-grained PAT (recommended) | **Pull requests: Read and write** | Scoped to specific repos. Create at *Settings -> Developer settings -> Fine-grained tokens*. |
| Classic PAT | `repo` scope | Broader access. Create at *Settings -> Developer settings -> Tokens (classic)*. |
| GitHub App installation token | `pull_requests: write` | Best for org-wide use. Register an app and generate installation tokens. |

The token is used to:
- Fetch PR metadata and diff (`GET /repos/{owner}/{repo}/pulls/{number}`)
- Post PR reviews with inline comments (`POST /repos/{owner}/{repo}/pulls/{number}/reviews`)
- Post fallback comments (`POST /repos/{owner}/{repo}/issues/{number}/comments`)

Store the token as a **Secret text** credential in Jenkins (e.g., `github-token`).

## Jenkins Setup

### Required Plugins

| Plugin | Purpose |
|---|---|
| [Pipeline](https://plugins.jenkins.io/workflow-aggregator/) | Pipeline DSL support |
| [Docker Pipeline](https://plugins.jenkins.io/docker-workflow/) | Run steps inside Docker containers |
| [Credentials](https://plugins.jenkins.io/credentials/) | Manage secrets |
| [GitHub Branch Source](https://plugins.jenkins.io/github-branch-source/) (recommended) | Auto-detect PR context (`CHANGE_ID`, `GIT_URL`) |
| [GitHub Pull Request Builder](https://plugins.jenkins.io/ghprb/) (alternative) | PR trigger with `ghprbPullId` |

### Credential Binding

| Credential ID | Type | Contents |
|---|---|---|
| `github-token` | Secret text | GitHub PAT with pull request permissions |

AWS: use **role-based credentials** on the Jenkins agent (instance profile / IRSA). Do not store long-lived AWS keys in Jenkins.

### PR Number Detection

The pipeline auto-detects the PR number from these environment variables (in order):

| Variable | Source |
|---|---|
| `CHANGE_ID` | GitHub Branch Source plugin / multibranch pipeline |
| `ghprbPullId` | GitHub Pull Request Builder plugin |
| `GITHUB_PR_NUMBER` | Explicit env var (e.g., from Generic Webhook Trigger) |
| `prNumber` parameter | Manual override via `claudeReview(prNumber: '42')` |

### Repo Auto-Detection

The `owner/repo` is auto-detected from these environment variables (in order):

| Variable | Source |
|---|---|
| `GITHUB_REPO` | Explicit env var |
| `GIT_URL` | Jenkins Git plugin (HTTPS or SSH URL) |
| `CHANGE_URL` | Multibranch pipeline |
| `githubRepo` parameter | Manual override via `claudeReview(githubRepo: 'owner/repo')` |

The regex `github\.com[:\\/]([^\\/]+\\/[^\\/]+?)(?:\.git)?$` parses both `https://github.com/owner/repo.git` and `git@github.com:owner/repo.git`.

## Configuration

All parameters are passed to `claudeReview()` (Shared Library) or set as environment variables (standalone).

| Parameter | Env Var | Default | Description |
|---|---|---|---|
| `githubTokenCredentialsId` | -- | `github-token` | Jenkins credentials ID for GitHub PAT |
| `awsRegion` | `AWS_REGION` | `us-east-1` | AWS region for Bedrock |
| `githubApiUrl` | `GITHUB_API_URL` | `https://api.github.com` | GitHub API base URL (for GitHub Enterprise) |
| `githubRepo` | `GITHUB_REPO` | *(auto-detected)* | GitHub repository (`owner/repo`) |
| `prNumber` | `PR_NUMBER` | *(auto-detected)* | Pull request number |
| `bedrockInferenceProfile` | `BEDROCK_INFERENCE_PROFILE` | *(auto)* | Bedrock inference profile ARN **or** Bedrock model ID passed to Claude Code `--model` |
| `claudeModel` | `CLAUDE_MODEL` | *(deprecated)* | Alias for `bedrockInferenceProfile` |
| `maxTokens` | `CLAUDE_MAX_TOKENS` | `16384` | Max output tokens |
| `includePatterns` | `INCLUDE_PATTERNS` | *(all files)* | Comma-separated globs to include (e.g. `*.py,*.js`) |
| `excludePatterns` | `EXCLUDE_PATTERNS` | *(none)* | Comma-separated globs to exclude (e.g. `*.lock,*.min.js`) |
| `maxDiffSize` | `MAX_DIFF_SIZE` | `100000` | Max diff size in bytes before truncation |
| `failOnFindings` | `FAIL_ON_FINDINGS` | `false` | Fail the build when critical findings are found |
| `dockerImage` | -- | `node:20-slim` | Docker image for the review container |

### Model Pinning

For production stability, pin your Bedrock model:

```groovy
claudeReview(
    bedrockInferenceProfile: 'us.anthropic.claude-sonnet-4-6',
    // ...
)
```

### GitHub Enterprise Server

Point the API URL to your instance:

```groovy
claudeReview(
    githubApiUrl: 'https://github.example.com/api/v3',
    // ...
)
```

## AWS Bedrock Authentication

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

You must also **enable Claude models** in the AWS Bedrock console (Model access -> Request access).

### Assume-Role Best Practice

For production Jenkins setups, prefer **IAM role assumption** over long-lived access keys:

1. Create an IAM role with the Bedrock policy above
2. Grant the Jenkins agent's IAM identity `sts:AssumeRole` on that role
3. Use the [AWS Credentials Plugin](https://plugins.jenkins.io/aws-credentials/) with an assume-role credential
4. The review script inherits the assumed role's temporary credentials from the environment

This avoids storing long-lived AWS secrets in Jenkins.

## Reusing Across Repos

Once set up as a Shared Library, any repo can add Claude Code reviews with a single Jenkinsfile stage:

```groovy
@Library('claude-code-reviewer') _

pipeline {
    agent any
    stages {
        stage('Code Review') {
            when { expression { env.CHANGE_ID != null } }
            steps {
                claudeReview()
            }
        }
    }
}
```

No need to copy scripts or install dependencies per-repo. The Shared Library handles everything.

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

1. **Fetches PR metadata** from GitHub API, including the head commit SHA
2. **Fetches PR diff** as raw unified diff text via the GitHub diff media type
3. **Filters files** by include/exclude glob patterns
4. **Truncates** large diffs to stay within model context limits
5. **Calls Claude Code** in non-interactive mode (`claude -p`) with structured JSON output
6. **Validates** inline comment line numbers against the actual diff
7. **Posts an atomic PR review** with summary + inline comments via `POST /repos/{owner}/{repo}/pulls/{number}/reviews`
8. If the atomic review fails (HTTP 422 — invalid comment position), falls back to: summary-only review + individual inline comments + issue comment for any remaining failures

### Severity Levels

| Level | Icon | Meaning |
|---|---|---|
| `critical` | :exclamation: | Must fix — bugs, security vulnerabilities |
| `warning` | :warning: | Should fix — potential issues, bad patterns |
| `suggestion` | :bulb: | Consider — improvements, alternatives |
| `nitpick` | :broom: | Minor — style, naming, formatting |

## Troubleshooting

### "No PR number detected"

The pipeline relies on `CHANGE_ID` (set by the GitHub Branch Source plugin), `ghprbPullId` (GitHub Pull Request Builder plugin), or `GITHUB_PR_NUMBER` env var. Ensure your Jenkins job is configured as a multibranch pipeline with GitHub source, or pass `prNumber` explicitly.

### "Could not determine GitHub repo"

Set the `githubRepo` parameter in `claudeReview()` or export `GITHUB_REPO` as an env var. The auto-detection parses `GIT_URL` or `CHANGE_URL` for `github.com` patterns.

### "Claude Code exited with code 1"

- Check that `CLAUDE_CODE_USE_BEDROCK=1` is set
- Verify AWS credentials are valid and have Bedrock permissions
- Ensure Claude models are enabled in your Bedrock region
- Check the Jenkins console output for Claude Code's stderr

### "GitHub API error 404"

- The `GITHUB_REPO` is wrong or the token doesn't have access to the repository
- The PR number doesn't exist — check `PR_NUMBER`

### "GitHub API error 403"

The GitHub token doesn't have sufficient permissions. Ensure the PAT has `repo` scope (classic) or **Pull requests: Read and write** (fine-grained).

### "GitHub API error 422" during review posting

This usually means an inline comment targets a line that isn't part of the PR diff. The script automatically falls back to posting comments individually, then as an issue comment for any that still fail. This is expected behavior for edge cases.

### Inline comments not appearing

GitHub's PR review API requires that inline comments target lines within the actual diff hunk. If a comment targets a line outside the diff, GitHub rejects it. The script validates comment positions against the diff before posting and falls back gracefully.

### Large PRs are slow or truncated

Increase `maxDiffSize` or use `includePatterns` / `excludePatterns` to focus the review on relevant files. Very large diffs may hit Bedrock's context limits.

### Docker image issues

The default `node:20-slim` image is extended with Python 3 at runtime. If your Jenkins agent doesn't have Docker, set `dockerImage` to a custom image or use the standalone Jenkinsfile without Docker.

## References

- [Claude Code Documentation](https://code.claude.com/docs)
- [Claude Code CLI Reference](https://code.claude.com/docs/en/cli-reference)
- [Claude Code Headless / CI Mode](https://code.claude.com/docs/en/headless)
- [Claude Code + Amazon Bedrock](https://code.claude.com/docs/en/amazon-bedrock)
- [claude-code-action](https://github.com/anthropics/claude-code-action) — Anthropic's GitHub Action (conceptual reference)
- [GitHub Pull Request Reviews API](https://docs.github.com/en/rest/pulls/reviews)
- [GitHub Pull Request Comments API](https://docs.github.com/en/rest/pulls/comments)
- [AWS Bedrock Claude Models](https://docs.aws.amazon.com/bedrock/latest/userguide/models-supported.html)

## License

MIT
