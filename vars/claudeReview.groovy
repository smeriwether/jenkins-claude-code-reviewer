#!/usr/bin/env groovy

/**
 * claudeReview — Jenkins Shared Library step
 *
 * Runs Claude Code as an automated MR reviewer via AWS Bedrock.
 * Posts review comments (summary note + inline discussions) to a GitLab merge request.
 *
 * Usage in a Jenkinsfile:
 *
 *   @Library('claude-code-reviewer') _
 *   claudeReview(
 *       gitlabTokenCredentialsId: 'gitlab-token',
 *       awsRegion: 'us-east-1',
 *       bedrockInferenceProfile: 'arn:aws:bedrock:us-east-1:123456789012:inference-profile/your-profile',
 *   )
 *
 * Parameters:
 *   gitlabTokenCredentialsId   - Jenkins credentials ID for GitLab PAT (type: Secret text)
 *   awsRegion                  - AWS region for Bedrock (default: us-east-1)
 *   gitlabApiUrl               - GitLab API base URL (default: https://gitlab.com/api/v4)
 *   bedrockInferenceProfile    - Bedrock inference profile ARN or Bedrock model ID to pass to Claude Code `--model`
 *   maxTokens                  - Max output tokens (default: 16384)
 *   includePatterns            - Comma-separated file globs to include (e.g. "*.py,*.js")
 *   excludePatterns            - Comma-separated file globs to exclude (e.g. "*.lock,*.min.js")
 *   maxDiffSize                - Max diff bytes before truncation (default: 100000)
 *   failOnFindings             - Fail the build on critical findings (default: false)
 *   dockerImage                - Docker image to run in (default: node:20-slim)
 *   gitlabProjectId            - GitLab project ID override (default: auto-detected)
 *   mrIid                      - MR IID override (default: auto-detected)
 *
 * AWS authentication note:
 * - This step intentionally does NOT bind AWS access key/secret key.
 * - It expects role-based auth to be provided by the Jenkins agent runtime (instance profile on EC2, IRSA on EKS, etc.).
 */

def call(Map config = [:]) {
    // Defaults
    def gitlabTokenCredentialsId  = config.get('gitlabTokenCredentialsId', 'gitlab-token')
    def awsRegion                 = config.get('awsRegion', 'us-east-1')
    def gitlabApiUrl              = config.get('gitlabApiUrl', 'https://gitlab.com/api/v4')
    def bedrockInferenceProfile   = config.get('bedrockInferenceProfile', config.get('claudeModel', ''))
    def maxTokens                 = config.get('maxTokens', '16384')
    def includePatterns           = config.get('includePatterns', '')
    def excludePatterns           = config.get('excludePatterns', '')
    def maxDiffSize               = config.get('maxDiffSize', '100000')
    def failOnFindings            = config.get('failOnFindings', false)
    def dockerImage               = config.get('dockerImage', 'node:20-slim')

    // Resolve MR IID and project ID from environment
    // Supports: GitLab Branch Source plugin (gitlabMergeRequestIid, gitlabSourceRepoName),
    //           explicit env vars, or manual overrides
    def mrIid = config.get('mrIid',
        env.gitlabMergeRequestIid ?: env.gitlabMergeRequestId ?: env.MR_IID ?: env.CHANGE_ID ?: '')
    def gitlabProjectId = config.get('gitlabProjectId',
        env.gitlabMergeRequestTargetProjectId ?: env.GITLAB_PROJECT_ID ?: '')

    if (!mrIid) {
        echo "claudeReview: No MR IID detected (gitlabMergeRequestIid / MR_IID / CHANGE_ID). Skipping."
        return
    }
    if (!gitlabProjectId) {
        error "claudeReview: Could not determine GitLab project ID. Set 'gitlabProjectId' parameter or GITLAB_PROJECT_ID env var."
    }

    echo "claudeReview: Reviewing MR !${mrIid} in project ${gitlabProjectId}"

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

        // Bind GitLab credentials and run the review script.
        // AWS credentials are expected to come from the environment (instance profile, IRSA, etc.).
        withCredentials([
            string(credentialsId: gitlabTokenCredentialsId, variable: 'GITLAB_TOKEN')
        ]) {
            def envVars = [
                "CLAUDE_CODE_USE_BEDROCK=1",
                "AWS_REGION=${awsRegion}",
                "GITLAB_API_URL=${gitlabApiUrl}",
                "GITLAB_PROJECT_ID=${gitlabProjectId}",
                "MR_IID=${mrIid}",
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
