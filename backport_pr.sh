#!/usr/bin/env bash
set -e

# Simple backport script - cherry-picks a main PR to tentacle
# Usage: ceph-backport-simple.sh <PR_NUMBER> [target_branch]
# Requires: gh CLI (https://cli.github.com/) - run 'gh auth login' first

CEPH_UPSTREAM="upstream"
TARGET_BRANCH="${2:-tentacle}"
PR_NUMBER="$1"

if [[ ! $PR_NUMBER =~ ^[0-9]+$ ]]; then
    echo "Usage: $0 <PR_NUMBER> [target_branch]"
    echo "Example: $0 67227 tentacle"
    exit 1
fi

LOCAL_BRANCH="wip-${PR_NUMBER}-${TARGET_BRANCH}"

echo "==> Fetching PR #${PR_NUMBER} info..."
PR_INFO=$(gh api "repos/ceph/ceph/pulls/${PR_NUMBER}")

PR_TITLE=$(echo "$PR_INFO" | jq -r '.title')
PR_MERGED=$(echo "$PR_INFO" | jq -r '.merged')
MERGE_COMMIT=$(echo "$PR_INFO" | jq -r '.merge_commit_sha')
BASE_SHA=$(echo "$PR_INFO" | jq -r '.base.sha')
HEAD_SHA=$(echo "$PR_INFO" | jq -r '.head.sha')

echo "==> PR #${PR_NUMBER}: $PR_TITLE"

if [ "$PR_MERGED" = "true" ]; then
    echo "==> PR is merged, using merge commit: $MERGE_COMMIT"
    CHERRY_PICK_RANGE="${MERGE_COMMIT}^..${MERGE_COMMIT}^2"
else
    echo "WARNING: PR is not merged yet, using head..base range"
    CHERRY_PICK_RANGE="${BASE_SHA}..${HEAD_SHA}"
fi

echo "==> Fetching latest ${TARGET_BRANCH} from ${CEPH_UPSTREAM}..."
git fetch "$CEPH_UPSTREAM" "refs/heads/${TARGET_BRANCH}"

if git show-ref --verify --quiet "refs/heads/$LOCAL_BRANCH"; then
    echo "ERROR: Branch $LOCAL_BRANCH already exists. Delete it first:"
    echo "  git branch -D $LOCAL_BRANCH"
    exit 1
fi

echo "==> Creating branch $LOCAL_BRANCH based on ${TARGET_BRANCH}..."
git checkout -b "$LOCAL_BRANCH" FETCH_HEAD

echo "==> Fetching merge commit..."
git fetch "$CEPH_UPSTREAM" "$MERGE_COMMIT"

echo "==> Cherry-picking..."
if ! git cherry-pick -x "$CHERRY_PICK_RANGE"; then
    echo ""
    echo "ERROR: Cherry-pick failed due to conflicts."
    echo "Resolve conflicts manually, then run:"
    echo "  git cherry-pick --continue"
    echo ""
    echo "When done, push with:"
    echo "  git push origin $LOCAL_BRANCH"
    exit 1
fi

echo ""
echo "==> Cherry-pick successful!"
echo "==> Branch: $LOCAL_BRANCH"
echo ""
echo "Next steps:"
echo "  1. Review changes: git log --oneline upstream/${TARGET_BRANCH}..HEAD"
echo "  2. Push: git push origin $LOCAL_BRANCH"
echo "  3. Open PR: gh pr create --base ${TARGET_BRANCH} --title \"${TARGET_BRANCH}: ${PR_TITLE}\""