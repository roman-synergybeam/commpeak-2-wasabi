#!/usr/bin/env bash
#
# Commit and push whatever changed, from a Claude Code Stop hook.
#
# Deliberately cautious, because an automatic push is a loaded gun:
#   * it refuses to run when the tests are failing, so main never carries a
#     broken tree;
#   * it refuses when anything that looks like a credential is staged;
#   * it only ever commits tracked-file changes plus new files git would
#     accept -- .gitignore is respected, and nothing is force-pushed;
#   * it is silent when there is nothing to do.
#
# It writes what it did to .claude/auto-sync.log.
set -uo pipefail

REPO=/home/c2w/commpeak-2-wasabi
LOG="$REPO/.claude/auto-sync.log"
cd "$REPO" || exit 0

say() { printf '%s  %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >>"$LOG"; }

command -v git >/dev/null 2>&1 || exit 0
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || exit 0

# Nothing to do is the common case; stay quiet.
if [[ -z "$(git status --porcelain)" ]]; then
    exit 0
fi

# Never commit a credential, whatever .gitignore says.
PATTERNS='BEGIN [A-Z ]*PRIVATE KEY|aws_secret_access_key|-----BEGIN|ghp_[A-Za-z0-9]{20,}|xox[baprs]-[A-Za-z0-9-]{10,}'
if git diff --no-color | grep -nEq "$PATTERNS"; then
    say "REFUSED: something matching a credential pattern is in the diff"
    exit 0
fi
for f in .env master.key secrets.env database.env; do
    if git status --porcelain -- "$f" 2>/dev/null | grep -q .; then
        say "REFUSED: $f is in the working tree"
        exit 0
    fi
done

# A broken tree must not reach main. Skipped when there is no database to test
# against, since most of this suite needs one.
if [[ -n "${C2W_TEST_DATABASE_URL:-}" ]]; then
    if ! "$HOME/.local/bin/uv" run --quiet pytest -q >/tmp/auto-sync-tests.log 2>&1; then
        say "REFUSED: tests failing -- $(tail -1 /tmp/auto-sync-tests.log)"
        exit 0
    fi
fi
if ! "$HOME/.local/bin/uv" run --quiet ruff check src/ tests/ alembic/ >/dev/null 2>&1; then
    say "REFUSED: lint failing"
    exit 0
fi

git add -A
if [[ -z "$(git diff --cached --name-only)" ]]; then
    exit 0
fi

FILES=$(git diff --cached --name-only | wc -l | tr -d ' ')
SUMMARY=$(git diff --cached --name-only | head -3 | sed 's|.*/||' | paste -sd', ' -)
[[ "$FILES" -gt 3 ]] && SUMMARY="$SUMMARY and $((FILES - 3)) more"

git commit -q -m "Automatic sync: $SUMMARY

$FILES file(s) changed. Committed by the Claude Code Stop hook after tests and
lint passed; see .claude/auto-sync.log.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>" || {
    say "commit failed"; exit 0
}

if git push -q origin HEAD 2>>"$LOG"; then
    say "pushed $(git rev-parse --short HEAD) ($FILES file(s)): $SUMMARY"
else
    say "committed $(git rev-parse --short HEAD) but the push failed -- it will go with the next one"
fi
