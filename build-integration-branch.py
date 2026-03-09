#!/usr/bin/env python3

"""
Builds integration branches by merging PRs found by numbers.

On release branches (quincy, reef, squid, tentacle), PRs are ported safely:
The script fetches the exact commits of the PR, cherry-picks them onto a 
temporary branch, and then merges that branch (--no-ff) into the release branch.
This guarantees no squashing (all commits are preserved), keeps upstream 'main' 
history out, and recreates the standard "Merge branch prs/..." commit structure.

Prerequisites:
  - GitHub CLI (`gh`): https://cli.github.com/
    Then run: `gh auth login`

Usage:
  ./build-integration-branch.py --pr 1234,5678,9012
  ./build-integration-branch.py my-label --pr 1234,5678
  ./build-integration-branch.py --pr 66055,66069,66240 
        --distros "centos9 rocky10 jammy noble" 
        --archs "x86_64" 
        --branch-name "wip-rocky10-branch-of-the-day"
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time

TIME_FORMAT = '%Y-%m-%d-%s'
CODENAMES = 'quincy reef squid tentacle'
REPO = "ceph/ceph"
PR_FIELDS = 'number,title,url,state,headRefName,baseRefName,author,labels'
MAX_RETRIES = 5
RETRY_DELAY = 3
LABEL_PRIORITY = [
    'build/ops',    # 1st - build system
    'core',         # 2nd - C++ core
    'common',
    'pybind',       # pybind before mgr modules
    'mgr',
    'orchestrator',
    'cephadm',
    'dashboard',
    'monitoring',   # prometheus etc - after mgr/dashboard
    'nfs',
    'rbd',
    'cephfs',
    'tests',        # tests last
]

def pr_priority(pr):
    labels = [l['name'] for l in pr.get('labels', [])]
    for i, label in enumerate(LABEL_PRIORITY):
        if label in labels:
            return i
    return len(LABEL_PRIORITY)  # unknown labels go at end

def run(cmd, **kw):
    return subprocess.run(cmd, text=True, **kw)

def git(*args, **kw):
    return run(['git', *args], **kw)

def gh(*args):
    result = run(['gh', *args], capture_output=True)
    if result.returncode != 0:
        print(f"gh error: {result.stderr.strip()}")
        sys.exit(1)
    return json.loads(result.stdout) if result.stdout.strip() else None

def preflight():
    if git('rev-parse', '--git-dir', capture_output=True).returncode != 0:
        sys.exit("Error: Not inside a git repository.")
    if not shutil.which('gh'):
        sys.exit("Error: GitHub CLI (gh) not installed.")
    if run(['gh', 'auth', 'status'], capture_output=True).returncode != 0:
        sys.exit("Error: Not authenticated. Run: gh auth login")

def get_postfix():
    postfix = "-" + time.strftime(TIME_FORMAT, time.localtime())
    branch = git('rev-parse', '--abbrev-ref', 'HEAD',
                 check=True, capture_output=True).stdout.strip()
    if branch in CODENAMES.split():
        postfix += '-' + branch
        print(f"Adding current branch name '-{branch}' as a postfix")
    return postfix

def fetch_prs(label, pr_numbers, skip_prs, repo, via_cherry_pick=False):
    prs, seen = [], set()

    for num in (pr_numbers or []):
        pr = gh('pr', 'view', str(num), '--repo', repo, '--json', PR_FIELDS)
        state = pr.get('state', 'unknown')
        if state not in ('OPEN', 'open', 'MERGED', 'merged'):
            print(f"Warning: PR#{num} is {state} — including anyway")
        elif state in ('MERGED', 'merged') and not via_cherry_pick:
            print(f"Warning: PR#{num} is already merged")
        seen.add(pr['number'])
        prs.append(pr)

    if label:
        labeled = gh('pr', 'list', '--repo', repo, '--label', label,
              '--json', PR_FIELDS, '--limit', '200',
              '--state', 'all') or []
        labeled = [p for p in labeled 
           if p['state'] in ('OPEN', 'open', 'MERGED', 'merged')]
        labeled.sort(key=lambda p: (pr_priority(p), p['number']))
        print(f"--- found {len(labeled)} PRs tagged with '{label}'")
        for pr in labeled:
            if pr['number'] not in seen and pr['number'] not in skip_prs:
                seen.add(pr['number'])
                prs.append(pr)

    return prs

def fetch_pr_ref(pr, repo):
    num = pr['number']
    repo_url = f'https://github.com/{repo}.git'
    ref = f'refs/pull/{num}/head'
    local_ref = f'prs/{num}'
    print(f'--- fetching {repo_url} {ref}')

    for attempt in range(1, MAX_RETRIES + 1):
        rc = git('fetch', repo_url, f'+{ref}:{local_ref}').returncode
        if rc == 0:
            break
        elif rc == 1:
            print(f"  retrying ({attempt}/{MAX_RETRIES})...")
            time.sleep(RETRY_DELAY)
        else:
            raise Exception(f"Fetch failed for PR#{num} (rc={rc})")
    else:
        raise Exception(f"PR#{num} failed after {MAX_RETRIES} retries")

    return local_ref

def get_pr_commits(pr_number, repo):
    """Get PR commits from GitHub API for display and cherry-picking."""
    data = gh('pr', 'view', str(pr_number), '--repo', repo, '--json', 'commits')
    if not data or 'commits' not in data:
        return []
    return [(c['oid'], c['messageHeadline']) for c in data['commits']]

def get_conflicting_files():
    result = git('status', '--porcelain', capture_output=True)
    conflicts = []
    for line in result.stdout.strip().splitlines():
        if not line or len(line) < 3: continue
        xy = line[:2]
        if 'U' in xy or xy in ('AA', 'DD'):
            conflicts.append(line[3:])
    return conflicts

# --- Conflict Resolution Helpers ---

def cherry_pick_in_progress():
    return git('rev-parse', '--cherry-pick-head', capture_output=True).returncode == 0

def interactive_resolve_cherry_pick():
    """
    Interactively guide the user through resolving cherry-pick conflicts.
    Returns True when the cherry-pick sequence is fully complete, False on abort.
    """
    def print_conflict_prompt():
        conflicts = get_conflicting_files()
        if conflicts:
            print(f'\n  *** CONFLICTS detected during cherry-pick:')
            for f in conflicts:
                print(f'  *** {f}')
        print(f'\n  Resolve conflicts, git add, then press ENTER to continue.')
        print(f'    ENTER  = continue')
        print(f'    abort  = stop everything\n')

    print_conflict_prompt()

    while True:
        try:
            response = input('  > ').strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False

        if response in ('abort', 'quit', 'q'):
            return False

        if not cherry_pick_in_progress():
            print('  ok (cherry-pick already completed)')
            return True

        conflicts = get_conflicting_files()
        if conflicts:
            print(f'  Still conflicting: {", ".join(conflicts)}')
            print_conflict_prompt()
            continue

        git('add', '-A')
        result = git('cherry-pick', '--continue', '--no-edit', capture_output=True)

        if result.returncode != 0:
            stderr = result.stderr or ""
            if "The previous cherry-pick is now empty" in stderr:
                # Use --skip to discard the now-empty commit and cleanly advance
                # to the next commit (or finish). Do NOT use `git commit --allow-empty`
                # here — that does not clear CHERRY_PICK_HEAD.
                print('  Commit is now empty (already applied), skipping...')
                result2 = git('cherry-pick', '--skip', capture_output=True)
                if result2.returncode != 0:
                    print(f'  cherry-pick --skip failed: {result2.stderr.strip()}')
                    print_conflict_prompt()
                    continue
                if not cherry_pick_in_progress():
                    return True
                print_conflict_prompt()
                continue
            elif "no cherry-pick or revert in progress" in stderr:
                return True
            else:
                print(f'  cherry-pick --continue failed: {stderr.strip()}')
                print_conflict_prompt()
                continue

        # cherry-pick --continue returned 0: entire sequence is done
        return True

def merge_in_progress():
    return git('rev-parse', '--verify', 'MERGE_HEAD', capture_output=True).returncode == 0

def interactive_resolve_merge():
    conflicts = get_conflicting_files()
    if conflicts:
        print(f'\n  *** CONFLICTS detected during merge:')
        for f in conflicts:
            print(f'  *** {f}')
        print(f'\n  Resolve conflicts, git add, then press ENTER.')
        print(f'    ENTER  = continue')
        print(f'    abort  = stop everything\n')

    while True:
        try:
            response = input('  > ').strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False

        if response in ('abort', 'quit', 'q'):
            return False

        if not merge_in_progress():
            return True

        if get_conflicting_files():
            print(f'  Still conflicting: {", ".join(get_conflicting_files())}')
            continue

        rc = git('merge', '--continue', '--no-edit').returncode
        if rc == 0:
            return True
        print('  merge --continue failed. Check if files are staged.')

# --- Core Logic ---

def build_metadata_message(prs, trailers):
    refs = ', '.join(f"prs/{pr['number']}" for pr in prs)
    message = f"Integration branch metadata\n\nMerged branches {refs}\n"
    if trailers:
        message += '\n' + '\n'.join(trailers) + '\n'
    return message

def apply_prs_to_release_branch(prs, repo, branch, original_branch, trailers):
    """The 'Cherry-Pick Bubble' approach for diverged release branches."""
    print(f'--- creating branch {branch} based on {original_branch}')
    git('branch', '-D', branch, capture_output=True)
    # Explicitly base the integration branch on original_branch to avoid
    # accidentally inheriting a stale or mispointed local branch.
    if git('checkout', '-b', branch, original_branch).returncode != 0:
        sys.exit(f"Failed to create branch {branch} from {original_branch}")

    for pr in prs:
        num = pr['number']
        print(f'\n{"="*50}')
        print(f'--- Processing PR #{num}: {pr["title"]}')

        local_ref = fetch_pr_ref(pr, repo)
        commits = get_pr_commits(num, repo)

        if not commits:
            print(f"--- Warning: No commits found for PR {num}, skipping.")
            continue

        shas = [c[0] for c in commits]
        print(f"--- Commits to cherry-pick:")
        for sha, headline in commits:
            print(f"      {sha[:12]}  {headline}")

        # 1. Create a temp branch right here to hold this PR's commits
        temp_pr_branch = f"_temp_pr_{num}"
        git('branch', '-D', temp_pr_branch, capture_output=True)
        git('checkout', '-b', temp_pr_branch)

        print(f"--- Cherry-picking {len(shas)} commit(s) from PR #{num}")
        rc = git('cherry-pick', *shas).returncode

        if rc != 0:
            # If it's only empty commits (already applied), skip entirely —
            # don't create an empty merge bubble.
            status = git('status', '--porcelain', capture_output=True).stdout
            if not status.strip() and not get_conflicting_files():
                print(f"  Commits already in branch, skipping PR #{num} entirely...")
                git('cherry-pick', '--abort', capture_output=True)
                git('checkout', branch)
                git('branch', '-D', temp_pr_branch, capture_output=True)
                continue
            else:
                resolved = interactive_resolve_cherry_pick()
                if not resolved:
                    print('--- aborting')
                    git('cherry-pick', '--abort', capture_output=True)
                    git('checkout', original_branch)
                    git('branch', '-D', branch, capture_output=True)
                    git('branch', '-D', temp_pr_branch, capture_output=True)
                    sys.exit(1)

        # 2. Move back to the integration branch and merge the temp branch.
        # This creates the exact "Merge branch prs/..." commit bubble.
        git('checkout', branch)
        merge_msg = f"Merge branch {local_ref}"
        print(f"--- Creating merge bubble: {merge_msg}")

        rc = git('merge', '--no-ff', '--no-edit', '-m', merge_msg, temp_pr_branch).returncode
        if rc != 0:
            print("--- Unexpected conflict creating the merge bubble. Please resolve.")
            if not interactive_resolve_merge():
                git('merge', '--abort', capture_output=True)
                git('checkout', original_branch)
                git('branch', '-D', branch, capture_output=True)
                git('branch', '-D', temp_pr_branch, capture_output=True)
                sys.exit(1)

        # Clean up the temp branch
        git('branch', '-D', temp_pr_branch, capture_output=True)
        print(f"--- Successfully processed PR #{num}")

    # 3. Final empty metadata commit at the very end
    print(f'\n--- creating final empty metadata commit')
    if git('commit', '--allow-empty', '-m', build_metadata_message(prs, trailers)).returncode != 0:
        sys.exit('Failed to create final metadata commit!')

def merge_direct(prs, repo, branch, original_branch, trailers):
    """Standard merge mode (usually for 'main' branch PRs onto 'main')."""
    print(f'--- creating branch {branch} based on {original_branch}')
    git('branch', '-D', branch, capture_output=True)
    # Explicitly base the integration branch on original_branch to avoid
    # accidentally inheriting a stale or mispointed local branch.
    if git('checkout', '-b', branch, original_branch).returncode != 0:
        sys.exit(f"Failed to create branch {branch} from {original_branch}")

    applied_prs = []
    for pr in prs:
        num = pr['number']
        print(f'\n{"="*50}')
        print(f'--- Processing PR #{num}: {pr["title"]}')
        local_ref = fetch_pr_ref(pr, repo)
        rc = git('merge', '--no-ff', '--no-edit', '-m', f"Merge branch {local_ref}", local_ref).returncode
        if rc != 0:
            if not interactive_resolve_merge():
                git('merge', '--abort', capture_output=True)
                git('checkout', original_branch)
                git('branch', '-D', branch, capture_output=True)
                sys.exit(1)
        applied_prs.append(pr)
    if git('commit', '--allow-empty', '-m', build_metadata_message(applied_prs, trailers)).returncode != 0:
        sys.exit('Failed to create final metadata commit!')

def parse_args():
    parser = argparse.ArgumentParser(usage=__doc__)
    parser.add_argument("label", nargs='?', default=None, help="GitHub label to search for")
    parser.add_argument("--pr", type=lambda v: [int(x) for x in v.split(',')], default=[], help="Comma-separated PR numbers")
    parser.add_argument("--skip-pr", type=lambda v: [int(x) for x in v.split(',')], default=[], help="Comma-separated PR numbers to skip (only with --label)")
    parser.add_argument("--branch-name", help="Override branch name")
    parser.add_argument("--no-date", "--no-postfix", action="store_true", help="Don't add date postfix to branch name")
    parser.add_argument("--repo", default=REPO)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--merge", action="store_true", help="Force direct merge even on release branches")
    parser.add_argument("--trailer", action="append", dest='trailers')
    parser.add_argument('--ceph-build-job', action="append", dest='trailers', type=lambda v: f'CEPH-BUILD-JOB: {v}')
    parser.add_argument('--distros', action="append", dest='trailers', type=lambda v: f'DISTROS: {v}')
    parser.add_argument('--archs', action="append", dest='trailers', type=lambda v: f'ARCHS: {v}')
    args = parser.parse_args()
    if not args.label and not args.pr:
        parser.error("Must specify either a label or --pr")
    return args

def main():
    cli = parse_args()
    preflight()

    original_branch = git('rev-parse', '--abbrev-ref', 'HEAD', check=True, capture_output=True).stdout.strip()
    via_cherry_pick = (not cli.merge and original_branch in CODENAMES.split())

    if via_cherry_pick:
        print(f"--- Release branch mode (base: {original_branch})")
    else:
        print(f"--- Merge mode")

    # Fetch PRs before the dry-run check so we can show warnings about
    # PRs targeting the wrong base branch and the full list that would be processed.
    prs = fetch_prs(cli.label, cli.pr, set(cli.skip_pr), cli.repo, via_cherry_pick=via_cherry_pick)
    if not prs:
        sys.exit("--- no PRs found, nothing to do")
    print(f"--- queued {len(prs)} PRs")

    base = cli.branch_name or cli.label or 'integration'
    branch = base if cli.no_date else base + get_postfix()

    if cli.dry_run:
        mode = "Cherry-Pick Bubble" if via_cherry_pick else "Direct Merge"
        print(f"\n--- dry-run: would create branch '{branch}' ({mode})")
        print(f"--- PRs to process:")
        for pr in prs:
            target = pr.get('baseRefName', '?')
            print(f"  [+] PR #{pr['number']} [{pr['state']}] (-> {target}) - {pr['title']} author: {pr.get('author', {}).get('login', 'unknown')}")
            print(f"      {pr['url']}")

        # Easy copy-paste for the next run, without merged or closed PRs that would be skipped anyway.
        pr_list = ','.join(str(pr['number']) for pr in prs if pr['state'] in ('OPEN', 'open'))
        print(f"\n--- copy-paste ready:")
        print(f"  --pr {pr_list}")

        print(f"\n--- metadata commit message would be:")
        print(build_metadata_message(prs, cli.trailers))
        return

    if via_cherry_pick:
        apply_prs_to_release_branch(prs, cli.repo, branch, original_branch, cli.trailers)
    else:
        merge_direct(prs, cli.repo, branch, original_branch, cli.trailers)

    print(f'\n{"=" * 60}\n  SUMMARY  ({len(prs)} PRs on {branch})\n{"=" * 60}')
    for pr in prs:
        num = pr['number']
        print(f'  [ok] PR #{num} - {pr["title"]}\n       {pr["url"]}')
    print(f'\n{"=" * 60}\n  ./run-make-check.sh && git push ci {branch}\n{"=" * 60}')

if __name__ == '__main__':
    main()