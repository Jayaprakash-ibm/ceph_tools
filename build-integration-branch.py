#!/usr/bin/env python3

"""
Builds integration branches by merging PRs found by numbers.

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
PR_FIELDS = 'number,title,url,state,headRefName'
MAX_RETRIES = 5
RETRY_DELAY = 3


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
        sys.exit("Error: GitHub CLI (gh) not installed. "
                 "See https://cli.github.com/")
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


def fetch_prs(label, pr_numbers, repo):
    prs, seen = [], set()

    if label:
        labeled = gh('pr', 'list', '--repo', repo, '--label', label,
                      '--json', PR_FIELDS, '--limit', '200') or []
        labeled.sort(key=lambda p: p['number'])
        print(f"--- found {len(labeled)} PRs tagged with {label}")
        for pr in labeled:
            if pr['number'] not in seen:
                seen.add(pr['number'])
                prs.append(pr)

    for num in (pr_numbers or []):
        if num in seen:
            continue
        pr = gh('pr', 'view', str(num), '--repo', repo,
                '--json', PR_FIELDS)
        if pr.get('state') not in ('OPEN', 'open'):
            print(f"Warning: PR#{num} is {pr.get('state', 'unknown')}")
        seen.add(pr['number'])
        prs.append(pr)

    return prs


def merge_pr(pr, repo):
    num = pr['number']
    repo_url = f'https://github.com/{repo}.git'
    ref = f'refs/pull/{num}/head'
    local_ref = f'prs/{num}'
    print(f'--- pr {num} --- fetching {repo_url} {ref}')

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

    rc = git('merge', '--no-ff', '--no-edit',
             '-m', f'Merge branch {local_ref}', local_ref).returncode
    if rc != 0:
        raise Exception(f"Merge conflict on PR#{num}")


def parse_args():
    parser = argparse.ArgumentParser(usage=__doc__)
    parser.add_argument("label", nargs='?', default=None,
                        help="GitHub label to search for")
    parser.add_argument("--pr", type=lambda v: [int(x) for x in v.split(',')],
                        default=[], help="Comma-separated PR numbers")
    parser.add_argument("--branch-name", help="Override branch name")
    parser.add_argument("--no-date", "--no-postfix", action="store_true",
                        help="Don't add date postfix to branch name")
    parser.add_argument("--repo", default=REPO)
    parser.add_argument("--trailer", action="append", dest='trailers')
    parser.add_argument('--ceph-build-job', action="append", dest='trailers',
                        type=lambda v: f'CEPH-BUILD-JOB: {v}')
    parser.add_argument('--distros', action="append", dest='trailers',
                        type=lambda v: f'DISTROS: {v}')
    parser.add_argument('--archs', action="append", dest='trailers',
                        type=lambda v: f'ARCHS: {v}')
    args = parser.parse_args()
    if not args.label and not args.pr:
        parser.error("Must specify either a label or --pr")
    return args


def main():
    cli = parse_args()
    preflight()

    original_branch = git('rev-parse', '--abbrev-ref', 'HEAD',
                          check=True, capture_output=True).stdout.strip()

    base = cli.branch_name or cli.label or 'integration'
    branch = base if cli.no_date else base + get_postfix()

    prs = fetch_prs(cli.label, cli.pr, cli.repo)
    if not prs:
        sys.exit("--- no PRs found, nothing to do")
    print(f"--- queried {len(prs)} prs")

    # Assemble branch
    print(f'--- creating branch {branch}')
    git('branch', '-D', branch, capture_output=True)  # silent if missing
    if git('checkout', '-b', branch).returncode != 0:
        sys.exit(f"Failed to create branch {branch}")

    try:
        for pr in prs:
            merge_pr(pr, cli.repo)
    except Exception as e:
        print(f'--- error: {e}')
        git('merge', '--abort', capture_output=True)
        git('checkout', original_branch)
        git('branch', '-D', branch, capture_output=True)
        sys.exit(1)

    refs = ', '.join(f"prs/{pr['number']}" for pr in prs)
    message = 'Merged branches ' + refs
    if cli.trailers:
        message += '\n' + '\n'.join(cli.trailers)
    cmd = ['git', 'commit', '--allow-empty', '--amend', '-m', message]
    if run(cmd).returncode != 0:
        sys.exit('Failed to amend final commit!')

    print('--- done. these PRs were included:')
    for pr in prs:
        print(f"  {pr['url']} - {pr['title']}")
    print(f'--- perhaps you want to: '
          f'./run-make-check.sh && git push ci {branch}')


if __name__ == '__main__':
    main()