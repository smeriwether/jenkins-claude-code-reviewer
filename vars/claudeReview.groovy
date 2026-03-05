#!/usr/bin/env groovy

/**
 * claudeReview — Jenkins Shared Library step
 *
 * Runs Claude Code as an automated PR reviewer via AWS Bedrock.
 * Posts review comments (summary + inline) to the GitHub pull request.
 *
 * Usage in a Jenkinsfile:
 *
 *   @Library('claude-code-reviewer') _
 *   claudeReview(
 *       awsCredentialsId: 'aws-bedrock-creds',
 *       githubTokenCredentialsId: 'github-token',
 *       awsRegion: 'us-east-1',
 *   )
 *
 * Parameters:
 *   awsCredentialsId          - Jenkins credentials ID for AWS (type: AWS Credentials or Username/Password)
 *   githubTokenCredentialsId  - Jenkins credentials ID for GitHub token (type: Secret text)
 *   awsRegion                 - AWS region for Bedrock (default: us-east-1)
 *   claudeModel               - Bedrock model ID (default: auto-selected by Claude Code)
 *   maxTokens                 - Max output tokens (default: 16384)
 *   includePatterns           - Comma-separated file globs to include (e.g. "*.py,*.js")
 *   excludePatterns           - Comma-separated file globs to exclude (e.g. "*.lock,*.min.js")
 *   maxDiffSize               - Max diff bytes before truncation (default: 100000)
 *   reviewEvent               - GitHub review event: COMMENT, APPROVE, REQUEST_CHANGES (default: COMMENT)
 *   failOnFindings            - Fail the build on critical findings (default: false)
 *   dockerImage               - Docker image to run in (default: node:20-slim)
 */

def call(Map config = [:]) {
    // Defaults
    def awsCredentialsId         = config.get('awsCredentialsId', 'aws-bedrock-creds')
    def githubTokenCredentialsId = config.get('githubTokenCredentialsId', 'github-token')
    def awsRegion                = config.get('awsRegion', 'us-east-1')
    def claudeModel              = config.get('claudeModel', '')
    def maxTokens                = config.get('maxTokens', '16384')
    def includePatterns          = config.get('includePatterns', '')
    def excludePatterns          = config.get('excludePatterns', '')
    def maxDiffSize              = config.get('maxDiffSize', '100000')
    def reviewEvent              = config.get('reviewEvent', 'COMMENT')
    def failOnFindings           = config.get('failOnFindings', false)
    def dockerImage              = config.get('dockerImage', 'node:20-slim')

    // Resolve PR number and repo from environment
    // Supports: GitHub Branch Source plugin, Generic Webhook Trigger, or manual override
    def prNumber = config.get('prNumber', env.CHANGE_ID ?: env.ghprbPullId ?: env.PR_NUMBER ?: '')
    def repository = config.get('repository', env.CHANGE_URL?.replaceAll('https://github.com/', '')?.replaceAll('/pull/.*', '') ?: env.GITHUB_REPOSITORY ?: '')

    if (!prNumber) {
        echo "claudeReview: No PR number detected (CHANGE_ID / ghprbPullId / PR_NUMBER). Skipping."
        return
    }
    if (!repository) {
        error "claudeReview: Could not determine GitHub repository. Set 'repository' parameter or GITHUB_REPOSITORY env var."
    }

    echo "claudeReview: Reviewing PR #${prNumber} in ${repository}"

    docker.image(dockerImage).inside('--entrypoint=""') {
        // Install Claude Code
        sh '''
            npm install -g @anthropic-ai/claude-code 2>&1
            claude --version
        '''

        // Bind credentials and run the review script
        withCredentials([
            string(credentialsId: githubTokenCredentialsId, variable: 'GITHUB_TOKEN'),
            usernamePassword(
                credentialsId: awsCredentialsId,
                usernameVariable: 'AWS_ACCESS_KEY_ID',
                passwordVariable: 'AWS_SECRET_ACCESS_KEY'
            )
        ]) {
            def envVars = [
                "CLAUDE_CODE_USE_BEDROCK=1",
                "AWS_REGION=${awsRegion}",
                "GITHUB_REPOSITORY=${repository}",
                "PR_NUMBER=${prNumber}",
                "CLAUDE_MODEL=${claudeModel}",
                "CLAUDE_MAX_TOKENS=${maxTokens}",
                "INCLUDE_PATTERNS=${includePatterns}",
                "EXCLUDE_PATTERNS=${excludePatterns}",
                "MAX_DIFF_SIZE=${maxDiffSize}",
                "REVIEW_EVENT=${reviewEvent}",
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
