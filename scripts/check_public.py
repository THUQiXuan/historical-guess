#!/usr/bin/env python3
"""Fail if tracked files or Git history contain credentials/private runtime data.

Only filenames/rule names are printed: never print a matched credential.
Run after `git add` and before commit/push. This is defense in depth; always
review `git diff --cached --stat` and never add local authentication files.
"""
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PATTERNS = {
    'github_token': rb'(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{30,})',
    'api_token': rb'\bsk-[A-Za-z0-9_-]{20,}',
    'aws_access': rb'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b',
    'google_api_key': rb'\bAIza[A-Za-z0-9_-]{30,}',
    'private_key': rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',
    'embedded_url_auth': rb'https?://[^\s/\x22\x27]+:[^\s/\x22\x27]+@',
}
FORBIDDEN_PARTS = {'.venv', 'var', '.codex', '__pycache__', '.pytest_cache', 'node_modules', 'test-results'}
FORBIDDEN_NAMES = {'auth.json', 'hosts.yml', 'credentials.json', '.env'}


def git(*args):
    return subprocess.check_output(['git', *args], cwd=ROOT)


def scan(label, content, failures):
    for name, pattern in PATTERNS.items():
        if re.search(pattern, content):
            failures.append((label, name))


def main():
    failures = []
    paths = git('ls-files', '-z').decode().split('\0')
    for name in filter(None, paths):
        path = Path(name)
        if (FORBIDDEN_PARTS.intersection(path.parts) or path.name in FORBIDDEN_NAMES
                or (path.name.startswith('.env.') and path.name != '.env.example')
                or re.search(r'\.(?:db|sqlite\w*|pem|key|log|jsonl)(?:-|$)', name)):
            failures.append((name, 'private_runtime_file'))
        # Read the staged version rather than trusting the worktree.
        scan(name, git('show', ':' + name), failures)
    env = git('show', ':.env.example').decode()
    for line in env.splitlines():
        if re.match(r'^\s*(?:OPENAI_API_KEY|GH_TOKEN|GITHUB_TOKEN)\s*=\s*[^\s#]', line):
            failures.append(('.env.example', 'nonempty_credential_example'))
    # Include every historical blob reachable from every local ref, even a
    # secret removed by a later commit. An empty repository has no old blobs.
    objects = git('rev-list', '--objects', '--all').decode().splitlines()
    seen = set()
    for item in objects:
        oid = item.split(' ', 1)[0]
        if oid in seen:
            continue
        seen.add(oid)
        if git('cat-file', '-t', oid).strip() == b'blob':
            scan('history-blob:' + oid[:12], git('cat-file', 'blob', oid), failures)
    if failures:
        for name, rule in failures:
            print(f'BLOCKED: {name}: {rule}')
        raise SystemExit(1)
    print(f'Public-content check passed: {len(list(filter(None, paths)))} tracked files; history inspected.')


if __name__ == '__main__':
    main()
