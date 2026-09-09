#!/usr/bin/env bash
#
# Commit and push whatever changed, from a Claude Code Stop hook.
#
# Deliberately cautious, because an automatic push is a loaded gun:
#   * it refuses when anything that looks like a credential is about to be
#     committed -- including in a brand-new untracked file;
#   * it refuses when the tests or the linter fail, so main never carries a
#     broken tree;
#   * it only ever commits what git would accept -- .gitignore is respected,
#     and nothing is force-pushed;
#   * it is silent when there is nothing to do.
#
# It writes what it did to .claude/auto-sync.log.
#
# The credential scan reads the *working tree*, not `git diff`. An earlier
# version scanned `git diff`, which reports tracked changes only, so the first
# thing it ever did was push a new file containing a fake AWS secret key. A new
# file is precisely the shape a leak arrives in.
set -uo pipefail

REPO=/home/c2w/commpeak-2-wasabi
LOG="$REPO/.claude/auto-sync.log"
UV="$HOME/.local/bin/uv"
cd "$REPO" || exit 0

say() { printf '%s  %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >>"$LOG"; }

command -v git >/dev/null 2>&1 || exit 0
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || exit 0

# Nothing to do is the common case; stay quiet.
[[ -z "$(git status --porcelain)" ]] && exit 0

# Never push from a detached HEAD or a rebase/merge in progress -- "push origin
# HEAD" from there lands somewhere nobody asked for.
BRANCH=$(git symbolic-ref --quiet --short HEAD) || {
    say "REFUSED: HEAD is detached"; exit 0
}
if [[ -e .git/MERGE_HEAD || -d .git/rebase-merge || -d .git/rebase-apply ]]; then
    say "REFUSED: a merge or rebase is in progress"
    exit 0
fi

# ---------------------------------------------------------------- credentials
# Everything git is about to commit: tracked modifications plus untracked,
# non-ignored files. -I skips binaries; a match anywhere stops the push.
PATTERNS='BEGIN [A-Z ]*PRIVATE KEY|-----BEGIN|aws_secret_access_key|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{20,}|xox[baprs]-[A-Za-z0-9-]{10,}|C2W_MASTER_KEY[=:]'

# Added lines only. A "-" line means a credential is being *removed*, which is
# the one change involving a credential that must always be allowed through.
if git diff HEAD --no-color | grep '^+' | grep -v '^+++' | grep -qE "$PATTERNS"; then
    say "REFUSED: a credential pattern is being added to a tracked file"
    exit 0
fi

UNTRACKED=$(git ls-files --others --exclude-standard)
if [[ -n "$UNTRACKED" ]]; then
    HIT=$(printf '%s\n' "$UNTRACKED" | tr '\n' '\0' \
          | xargs -0 -r grep -lIE "$PATTERNS" 2>/dev/null | head -3 | paste -sd', ' -)
    if [[ -n "$HIT" ]]; then
        say "REFUSED: a credential pattern is in new file(s): $HIT"
        exit 0
    fi
fi

for f in .env master.key secrets.env database.env; do
    if git status --porcelain -- "$f" 2>/dev/null | grep -q .; then
        say "REFUSED: $f is in the working tree"
        exit 0
    fi
done

# ---------------------------------------------------------------------- tests
# The database-backed suite (RLS isolation, pipeline, web) skips itself when it
# has no database, so pytest alone cannot tell us whether it really ran. Find a
# database if one is configured, and record in the log which of the two
# happened -- a green line that silently covered a third of the suite is worse
# than a line that admits the gap.
DB="${C2W_TEST_DATABASE_URL:-}"
[[ -z "$DB" && -r "$HOME/.config/c2w/test-database-url" ]] \
    && DB=$(tr -d '[:space:]' <"$HOME/.config/c2w/test-database-url")

# A configured-but-unreachable database is a landmine: the suite would error
# instead of skipping, and this hook would then refuse every commit forever.
# Probe it, and fall back to skipping rather than blocking.
if [[ -n "$DB" ]]; then
    HOSTPORT=$(printf '%s' "$DB" | sed -E 's|^.*://||; s|^[^@]*@||; s|/.*$||')
    PROBE_HOST=${HOSTPORT%%:*}
    PROBE_PORT=${HOSTPORT##*:}
    [[ "$PROBE_PORT" == "$PROBE_HOST" ]] && PROBE_PORT=5432
    if ! timeout 3 bash -c "exec 3<>/dev/tcp/$PROBE_HOST/$PROBE_PORT" 2>/dev/null; then
        say "note: test database at $HOSTPORT is unreachable; running without it"
        DB=""
    fi
fi

TESTLOG=$(mktemp)
if ! C2W_TEST_DATABASE_URL="$DB" "$UV" run --quiet pytest -q >"$TESTLOG" 2>&1; then
    say "REFUSED: tests failing -- $(grep -E '^(FAILED|ERROR)' "$TESTLOG" | head -1 || tail -1 "$TESTLOG")"
    rm -f "$TESTLOG"
    exit 0
fi
COUNTS=$(tail -3 "$TESTLOG" | grep -oE '[0-9]+ (passed|skipped|failed)' | paste -sd', ' -)
rm -f "$TESTLOG"
if [[ -n "$DB" ]]; then
    VERIFIED="tests: $COUNTS (with a database)"
else
    VERIFIED="tests: $COUNTS -- NO DATABASE, so the RLS/pipeline/web suites skipped"
fi

if ! "$UV" run --quiet ruff check src/ tests/ alembic/ >/dev/null 2>&1; then
    say "REFUSED: lint failing"
    exit 0
fi

# --------------------------------------------------------------------- commit
git add -A
[[ -z "$(git diff --cached --name-only)" ]] && exit 0

FILES=$(git diff --cached --name-only | wc -l | tr -d ' ')
SUMMARY=$(git diff --cached --name-only | head -3 | sed 's|.*/||' | paste -sd', ' -)
[[ "$FILES" -gt 3 ]] && SUMMARY="$SUMMARY and $((FILES - 3)) more"

git commit -q -m "Automatic sync: $SUMMARY

$FILES file(s) changed. Committed by the Claude Code Stop hook.
$VERIFIED
Lint clean. See .claude/auto-sync.log.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>" || {
    say "commit failed"; exit 0
}

if git push -q origin "$BRANCH" 2>>"$LOG"; then
    say "pushed $(git rev-parse --short HEAD) -> $BRANCH ($FILES file(s)): $SUMMARY | $VERIFIED"
else
    say "committed $(git rev-parse --short HEAD) but the push failed -- it will go with the next one"
fi
