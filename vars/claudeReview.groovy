#!/usr/bin/env groovy

/**
 * claudeReview — Jenkins Shared Library step
 *
 * Runs Claude Code as an automated PR reviewer via AWS Bedrock.
 * Posts review comments (summary + inline comments) to a GitHub pull request.
 *
 * Usage in a Jenkinsfile:
 *
 *   @Library('claude-code-reviewer') _
 *   claudeReview(
 *       githubTokenCredentialsId: 'github-token',
 *       awsRegion: 'us-east-1',
 *       bedrockInferenceProfile: 'arn:aws:bedrock:us-east-1:123456789012:inference-profile/your-profile',
 *   )
 *
 * Parameters:
 *   githubTokenCredentialsId   - Jenkins credentials ID for GitHub PAT (type: Secret text)
 *   awsRegion                  - AWS region for Bedrock (default: us-east-1)
 *   githubApiUrl               - GitHub API base URL (default: https://api.github.com)
 *   bedrockInferenceProfile    - Bedrock inference profile ARN or Bedrock model ID to pass to Claude Code `--model`
 *   maxTokens                  - Max output tokens (default: 16384)
 *   includePatterns            - Comma-separated file globs to include (e.g. "*.py,*.js")
 *   excludePatterns            - Comma-separated file globs to exclude (e.g. "*.lock,*.min.js")
 *   maxDiffSize                - Max diff bytes before truncation (default: 100000)
 *   failOnFindings             - Fail the build on critical findings (default: false)
 *   dockerImage                - Docker image to run in (default: node:20-slim)
 *   githubRepo                 - GitHub repo override (format: owner/repo; default: auto-detected from GIT_URL)
 *   prNumber                   - PR number override (default: auto-detected)
 *   awsCredentialsId           - Jenkins credentials ID for AWS keys (type: Username/Password; default: '' = use role-based auth)
 */

def call(Map config = [:]) {
    // Defaults
    def githubTokenCredentialsId  = config.get('githubTokenCredentialsId', 'github-token')
    def awsRegion                 = config.get('awsRegion', 'us-east-1')
    def githubApiUrl              = config.get('githubApiUrl', 'https://api.github.com')
    def bedrockInferenceProfile   = config.get('bedrockInferenceProfile', config.get('claudeModel', ''))
    def maxTokens                 = config.get('maxTokens', '16384')
    def includePatterns           = config.get('includePatterns', '')
    def excludePatterns           = config.get('excludePatterns', '')
    def maxDiffSize               = config.get('maxDiffSize', '100000')
    def failOnFindings            = config.get('failOnFindings', false)
    def dockerImage               = config.get('dockerImage', 'node:20-slim')
    def awsCredentialsId          = config.get('awsCredentialsId', '')

    // Resolve PR number from environment
    // Supports: GitHub Branch Source plugin (CHANGE_ID), GitHub Pull Request Builder (ghprbPullId),
    //           explicit env var (GITHUB_PR_NUMBER), or manual override
    def prNumber = config.get('prNumber',
        env.CHANGE_ID ?: env.ghprbPullId ?: env.GITHUB_PR_NUMBER ?: '')

    // Resolve GitHub repo (owner/repo) from environment
    def githubRepo = config.get('githubRepo', _detectGithubRepo())

    if (!prNumber) {
        echo "claudeReview: No PR number detected (CHANGE_ID / ghprbPullId / GITHUB_PR_NUMBER). Skipping."
        return
    }
    if (!githubRepo) {
        error "claudeReview: Could not determine GitHub repo. Set 'githubRepo' parameter or GITHUB_REPO env var."
    }

    echo "claudeReview: Reviewing PR #${prNumber} in ${githubRepo}"

    docker.image(dockerImage).inside('--entrypoint=""') {
        // Install dependencies inside the container
        sh '''
            set -euxo pipefail
            # node:*-slim images don't include Python; we need it for the review script.
            apt-get update
            apt-get install -y --no-install-recommends python3 ca-certificates git curl
            rm -rf /var/lib/apt/lists/*

            npm install -g @anthropic-ai/claude-code 2>&1
            claude --version
            python3 --version
        '''

        // Build credential bindings: always bind GitHub token, optionally bind AWS keys
        def credBindings = [
            string(credentialsId: githubTokenCredentialsId, variable: 'GITHUB_TOKEN')
        ]
        if (awsCredentialsId) {
            credBindings.add(usernamePassword(
                credentialsId: awsCredentialsId,
                usernameVariable: 'AWS_ACCESS_KEY_ID',
                passwordVariable: 'AWS_SECRET_ACCESS_KEY'
            ))
        }

        withCredentials(credBindings) {
            def envVars = [
                "CLAUDE_CODE_USE_BEDROCK=1",
                "AWS_REGION=${awsRegion}",
                "GITHUB_API_URL=${githubApiUrl}",
                "GITHUB_REPO=${githubRepo}",
                "PR_NUMBER=${prNumber}",
                "BEDROCK_INFERENCE_PROFILE=${bedrockInferenceProfile}",
                "CLAUDE_MODEL=${bedrockInferenceProfile}",
                "CLAUDE_MAX_TOKENS=${maxTokens}",
                "INCLUDE_PATTERNS=${includePatterns}",
                "EXCLUDE_PATTERNS=${excludePatterns}",
                "MAX_DIFF_SIZE=${maxDiffSize}",
                "FAIL_ON_FINDINGS=${failOnFindings}",
            ]

            withEnv(envVars) {
                // Copy review script into workspace
                writeFile file: 'claude_review.py', text: libraryResource('scripts/review.py')

                sh 'python3 claude_review.py'
            }
        }
    }
}

/**
 * Auto-detect GitHub repo (owner/repo) from environment variables.
 * Parses GIT_URL or CHANGE_URL for github.com patterns.
 */
private String _detectGithubRepo() {
    // Check explicit env var first
    if (env.GITHUB_REPO) {
        return env.GITHUB_REPO
    }

    // Try GIT_URL (set by Jenkins Git plugin)
    def gitUrl = env.GIT_URL ?: ''
    def repo = _parseGithubRepo(gitUrl)
    if (repo) return repo

    // Try CHANGE_URL (set by multibranch pipelines)
    def changeUrl = env.CHANGE_URL ?: ''
    repo = _parseGithubRepo(changeUrl)
    if (repo) return repo

    return ''
}

/**
 * Parse owner/repo from a GitHub URL (HTTPS or SSH).
 */
private String _parseGithubRepo(String url) {
    if (!url) return ''
    def matcher = url =~ /github\.com[:\\/]([^\\/]+\/[^\\/]+?)(?:\.git)?$/
    if (matcher.find()) {
        return matcher.group(1)
    }
    return ''
}
