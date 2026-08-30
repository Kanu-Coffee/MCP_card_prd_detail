#!/usr/bin/env bash
set -euo pipefail

test -n "${GH_TOKEN:-}"

action_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
repository_json=$(mktemp)
environment_json=$(mktemp)
policies_json=$(mktemp)
trap 'rm -f "$repository_json" "$environment_json" "$policies_json"' EXIT

gh api --method GET \
  -H "Accept: application/vnd.github+json" \
  -H "X-GitHub-Api-Version: 2026-03-10" \
  "repos/${GITHUB_REPOSITORY}" \
  > "$repository_json"
gh api --method GET \
  -H "Accept: application/vnd.github+json" \
  -H "X-GitHub-Api-Version: 2026-03-10" \
  "repos/${GITHUB_REPOSITORY}/environments/dockerhub-public" \
  > "$environment_json"
gh api --method GET \
  -H "Accept: application/vnd.github+json" \
  -H "X-GitHub-Api-Version: 2026-03-10" \
  "repos/${GITHUB_REPOSITORY}/environments/dockerhub-public/deployment-branch-policies?per_page=100" \
  > "$policies_json"

jq -e \
  --slurpfile repository "$repository_json" \
  --slurpfile policies "$policies_json" \
  -f "$action_dir/validate-environment.jq" \
  "$environment_json" >/dev/null
