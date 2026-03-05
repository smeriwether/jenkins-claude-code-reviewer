# Jenkins Claude Code Reviewer (GitLab)

A reusable Jenkins pipeline that runs [Claude Code](https://code.claude.com) as an automated code reviewer on **GitLab merge requests**, powered by **AWS Bedrock**.

Drop it into any repo. Claude reviews the MR diff, posts a summary note and inline discussion comments directly on the merge request.

> **Note:** This implementation targets GitLab MR workflows. The original concept was inspired by GitHub PR review patterns (see [claude-code-action](https://github.com/anthropics/claude-code-action)), but all API integration here is GitLab-native.

## How It Works

1. A merge request triggers the Jenkins pipeline
2. The pipeline installs Claude Code in a Docker container
3. The MR diff is fetched from GitLab API and filtered by file patterns
4. Claude Code analyzes the diff via AWS Bedrock (no Anthropic API key needed)
5. A summary note is posted to the MR, plus inline discussion comments on changed lines

## Prerequisites

- **Jenkins** with Pipeline and Docker Pipeline plugins
- **Docker** available on the Jenkins agent
- **AWS account** with Bedrock access and Claude models enabled
- **GitLab personal access token** with `api` scope
- **Docker** available on the Jenkins agent (default image: `node:20-slim`)

> AWS auth: this project is designed for **role-based AWS credentials** (instance profile / IRSA / assumed role). It does **not** require static access keys.

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
   - `gitlab-token` → *Secret text* — your GitLab PAT (requires `api` scope)

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
                    env.gitlabMergeRequestIid != null ||
                    env.MR_IID != null
                }
            }
            steps {
                claudeReview(
                    gitlabTokenCredentialsId: 'gitlab-token',
                    awsRegion: 'us-east-1',
                    bedrockInferenceProfile: 'arn:aws:bedrock:us-east-1:123456789012:inference-profile/your-profile',
                    // gitlabProjectId: '12345',  // if not auto-detected
                )
            }
        }
    }
}
```

### Option B: Standalone Jenkinsfile

Copy `scripts/review.py` and `examples/Jenkinsfile.standalone` into your repo. No Shared Library setup required. See the [standalone example](examples/Jenkinsfile.standalone) for details.

## GitLab Authentication

The review script authenticates to GitLab using a **Personal Access Token (PAT)**.

| Token type | Required scope | Notes |
|---|---|---|
| Personal Access Token (recommended) | `api` | Full API access. Create at *User Settings → Access Tokens*. |
| Project Access Token | `api` | Scoped to one project. Create at *Project → Settings → Access Tokens*. |
| Group Access Token | `api` | Scoped to a group. Create at *Group → Settings → Access Tokens*. |

The token is used to:
- Fetch MR metadata and diff (`GET /projects/:id/merge_requests/:iid/changes`)
- Post summary notes (`POST /projects/:id/merge_requests/:iid/notes`)
- Post inline discussion comments (`POST /projects/:id/merge_requests/:iid/discussions`)

Store the token as a **Secret text** credential in Jenkins (e.g., `gitlab-token`).

## Jenkins Setup

### Required Plugins

| Plugin | Purpose |
|---|---|
| [Pipeline](https://plugins.jenkins.io/workflow-aggregator/) | Pipeline DSL support |
| [Docker Pipeline](https://plugins.jenkins.io/docker-workflow/) | Run steps inside Docker containers |
| [Credentials](https://plugins.jenkins.io/credentials/) | Manage secrets |
| [GitLab Branch Source](https://plugins.jenkins.io/gitlab-branch-source/) (optional) | Auto-detect MR context (`gitlabMergeRequestIid`, project ID) |
| [GitLab Plugin](https://plugins.jenkins.io/gitlab-plugin/) (optional) | Alternative MR trigger with webhook integration |

### Credential Binding

| Credential ID | Type | Contents |
|---|---|---|
| `gitlab-token` | Secret text | GitLab PAT with `api` scope |

AWS: use **role-based credentials** on the Jenkins agent (instance profile / IRSA). Do not store long-lived AWS keys in Jenkins.

### MR Detection

The pipeline auto-detects the MR context from these environment variables (in order):

| Variable | Source |
|---|---|
| `gitlabMergeRequestIid` | GitLab Branch Source plugin |
| `gitlabMergeRequestId` | GitLab Plugin (webhook trigger) |
| `MR_IID` | Explicit env var (e.g., from Generic Webhook Trigger) |
| `CHANGE_ID` | Jenkins multibranch pipeline |

For the project ID:

| Variable | Source |
|---|---|
| `gitlabMergeRequestTargetProjectId` | GitLab Branch Source plugin |
| `GITLAB_PROJECT_ID` | Explicit env var or `claudeReview(gitlabProjectId: '...')` |

## Configuration

All parameters are passed to `claudeReview()` (Shared Library) or set as environment variables (standalone).

| Parameter | Env Var | Default | Description |
|---|---|---|---|
| `gitlabTokenCredentialsId` | — | `gitlab-token` | Jenkins credentials ID for GitLab PAT |
| `awsRegion` | `AWS_REGION` | `us-east-1` | AWS region for Bedrock |
| `gitlabApiUrl` | `GITLAB_API_URL` | `https://gitlab.com/api/v4` | GitLab API base URL (for self-hosted) |
| `gitlabProjectId` | `GITLAB_PROJECT_ID` | *(auto-detected)* | GitLab project ID (numeric or URL-encoded path) |
| `mrIid` | `MR_IID` | *(auto-detected)* | Merge request IID |
| `bedrockInferenceProfile` | `BEDROCK_INFERENCE_PROFILE` | *(auto)* | Bedrock inference profile ARN **or** Bedrock model ID passed to Claude Code `--model` |
| `claudeModel` | `CLAUDE_MODEL` | *(deprecated)* | Alias for `bedrockInferenceProfile` |
| `maxTokens` | `CLAUDE_MAX_TOKENS` | `16384` | Max output tokens |
| `includePatterns` | `INCLUDE_PATTERNS` | *(all files)* | Comma-separated globs to include (e.g. `*.py,*.js`) |
| `excludePatterns` | `EXCLUDE_PATTERNS` | *(none)* | Comma-separated globs to exclude (e.g. `*.lock,*.min.js`) |
| `maxDiffSize` | `MAX_DIFF_SIZE` | `100000` | Max diff size in bytes before truncation |
| `failOnFindings` | `FAIL_ON_FINDINGS` | `false` | Fail the build when critical findings are found |
| `dockerImage` | — | `node:20-slim` | Docker image for the review container |

### Model Pinning

For production stability, pin your Bedrock model:

```groovy
claudeReview(
    claudeModel: 'us.anthropic.claude-sonnet-4-6',
    // ...
)
```

### Self-Hosted GitLab

Point the API URL to your instance:

```groovy
claudeReview(
    gitlabApiUrl: 'https://gitlab.example.com/api/v4',
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

You must also **enable Claude models** in the AWS Bedrock console (Model access → Request access).

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
            when { expression { env.gitlabMergeRequestIid != null } }
            steps {
                claudeReview(
                    gitlabProjectId: '12345',  // your project's ID
                )
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

1. **Fetches MR metadata** from GitLab API, including `diff_refs` (base/head/start SHAs)
2. **Fetches MR changes** with per-file diffs via `/merge_requests/:iid/changes`
3. **Reconstructs unified diff** from the changes for Claude to review
4. **Filters files** by include/exclude glob patterns
5. **Truncates** large diffs to stay within model context limits
6. **Calls Claude Code** in non-interactive mode (`claude -p`) with structured JSON output
7. **Validates** inline comment line numbers against the actual diff
8. **Posts a summary note** to the MR via `POST /merge_requests/:iid/notes`
9. **Posts inline discussions** on changed lines via `POST /merge_requests/:iid/discussions` with position info
10. If an inline comment fails (position mismatch), it falls back to a note with file:line references

### Severity Levels

| Level | Icon | Meaning |
|---|---|---|
| `critical` | ❗ | Must fix — bugs, security vulnerabilities |
| `warning` | ⚠️ | Should fix — potential issues, bad patterns |
| `suggestion` | 💡 | Consider — improvements, alternatives |
| `nitpick` | 🧹 | Minor — style, naming, formatting |

## Troubleshooting

### "No MR IID detected"

The pipeline relies on `gitlabMergeRequestIid` (set by the GitLab Branch Source plugin) or `MR_IID` / `CHANGE_ID` env vars. Ensure your Jenkins job is configured as a multibranch pipeline with the GitLab source, or pass `MR_IID` explicitly.

### "Could not determine GitLab project ID"

Set the `gitlabProjectId` parameter in `claudeReview()` or export `GITLAB_PROJECT_ID` as an env var. You can find the project ID on the GitLab project page (Settings → General) or in the URL bar of the API.

### "Claude Code exited with code 1"

- Check that `CLAUDE_CODE_USE_BEDROCK=1` is set
- Verify AWS credentials are valid and have Bedrock permissions
- Ensure Claude models are enabled in your Bedrock region
- Check the Jenkins console output for Claude Code's stderr

### "GitLab API error 404"

- The `GITLAB_PROJECT_ID` is wrong or the token doesn't have access to the project
- The MR IID doesn't exist — check `MR_IID` is the *internal* ID (shown as `!123` in GitLab), not the global ID

### "GitLab API error 403"

The GitLab token doesn't have sufficient permissions. Ensure the PAT has the `api` scope. For project/group tokens, ensure the token role has at least Reporter access.

### Inline comments not appearing

GitLab's inline discussion API requires exact position info (`base_sha`, `head_sha`, `start_sha`, `new_path`, `new_line`). If the position doesn't match the MR diff exactly, GitLab rejects the comment. The script falls back to posting these as a note with file:line references. This is expected behavior for rebased or amended commits.

### Large MRs are slow or truncated

Increase `maxDiffSize` or use `includePatterns` / `excludePatterns` to focus the review on relevant files. Very large diffs may hit Bedrock's context limits.

### Docker image issues

The default `node:20-slim` image includes Node.js and Python 3. If your Jenkins agent doesn't have Docker, set `dockerImage` to a custom image or use the standalone Jenkinsfile without Docker.

## References

- [Claude Code Documentation](https://code.claude.com/docs)
- [Claude Code CLI Reference](https://code.claude.com/docs/en/cli-reference)
- [Claude Code Headless / CI Mode](https://code.claude.com/docs/en/headless)
- [Claude Code + Amazon Bedrock](https://code.claude.com/docs/en/amazon-bedrock)
- [claude-code-action](https://github.com/anthropics/claude-code-action) — Anthropic's GitHub Action (conceptual reference)
- [GitLab MR Notes API](https://docs.gitlab.com/ee/api/notes.html#create-new-merge-request-note)
- [GitLab MR Discussions API](https://docs.gitlab.com/ee/api/discussions.html#create-new-merge-request-thread)
- [GitLab MR Changes API](https://docs.gitlab.com/ee/api/merge_requests.html#get-single-mr-changes)
- [AWS Bedrock Claude Models](https://docs.aws.amazon.com/bedrock/latest/userguide/models-supported.html)

## License

MIT
