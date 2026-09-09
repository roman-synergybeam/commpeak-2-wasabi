"""The auto-sync hook's credential guard, tested as code rather than by eye.

``.claude/auto-sync.sh`` refuses to commit when a credential is about to be
pushed. That guard has failed open twice, both times silently:

* it scanned ``git diff``, which reports tracked changes only, so a brand-new
  file -- the shape a real leak takes -- was never examined;
* its pattern list was rewritten to start with ``-----BEGIN``, which ``grep``
  parsed as command-line options, so it matched nothing whatsoever.

Neither failure was visible from reading the script, and the second one made
every leak pass while the log stayed green. So the pattern list is now asserted
here: the tests extract it from the hook itself and run the real ``grep``
invocation, which means an edit to the regex that breaks it fails the suite --
and the hook refuses to push on a failing suite.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parent.parent / ".claude" / "auto-sync.sh"

# Resolved once, so the subprocess calls below name a full path rather than
# trusting PATH -- the hook runs unattended and so does this.
BASH = shutil.which("bash")
GREP = shutil.which("grep")

# Every fixture below is assembled from two halves at import time, and that is
# deliberate: written as literals they are exactly what the guard is built to
# stop, so the guard blocked this very file and the push with it. Splitting them
# keeps the guard maximally strict -- no path allowlist, no exemption for
# "tests" -- while still handing it the real string to match. The last test in
# this module asserts the file on disk stays clean.
MUST_BLOCK = [
    ("aws secret, shell style", "aws_secret_access_key = " + "wJalrXUtnFEMI" + "K7MDENGbPxRfiCYEX"),
    ("aws secret, yaml style", 'aws_secret_access_key: "' + "wJalrXUtnFEMIK7MDENGbPxRfiCYEX" + '"'),
    ("aws access key id", "AKIA" + "IOSFODNN7EXAMPLE"),
    ("openssh private key", "-----BEGIN " + "OPENSSH PRIVATE KEY-----"),
    ("rsa private key", "-----BEGIN " + "RSA PRIVATE KEY-----"),
    ("github token", "ghp_" + "abcdefghijklmnopqrstuvwxyz0123"),
    ("slack bot token", "xoxb-" + "123456789012-abcdefghij"),
    ("master key assignment", "C2W_MASTER_KEY=" + "6xJ2kQm8vTnR4wZs9pLc0aYe"),
    ("database url with a password", "postgresql://c2w:" + "S3cretPassw0rd@db.internal/c2w"),
]

MUST_PASS = [
    (
        "prose naming the key names",
        "a private key, an AKIA... id, aws_secret_access_key, or C2W_MASTER_KEY=.",
    ),
    ("the master key env var", "C2W_MASTER_KEY(_FILE), which come from the systemd unit"),
    ("database url with no password", "postgresql+asyncpg://c2w@127.0.0.1:5432/c2w_test"),
    ("an ordinary https url", "https://docs.commpeak.com/llms.txt"),
    ("a settings key name", 'get_secret(session, "alerts.telegram_bot_token")'),
    ("an empty credential setting", "aws_secret_access_key ="),
    ("a sealed column name", "cdr_api_key_sealed = mapped_column(LargeBinary)"),
]


def _patterns() -> str:
    """Pull the live pattern list out of the hook by running its own assignments.

    Reading it with a regex would test a copy. Executing the assignment lines
    tests what the hook actually greps with.
    """
    if not HOOK.exists():  # pragma: no cover - the hook is part of the repo
        pytest.skip("auto-sync hook is not present")
    lines = [
        ln for ln in HOOK.read_text().splitlines()
        if re.match(r"^PATTERNS(\+)?=", ln)
    ]
    assert lines, "no PATTERNS assignment found in the hook"
    out = subprocess.run(  # noqa: S603 - the input is this repo's own hook
        [BASH, "-c", "\n".join(lines) + '\nprintf %s "$PATTERNS"'],
        capture_output=True, text=True, check=True,
    )
    return out.stdout


def _blocks(patterns: str, text: str) -> bool:
    """Exactly the check the hook makes, including the ``-e`` that it needs."""
    proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [GREP, "-qE", "-e", patterns],
        input=text + "\n", capture_output=True, text=True,
    )
    assert proc.returncode in (0, 1), proc.stderr
    return proc.returncode == 0


@pytest.mark.parametrize("name,text", MUST_BLOCK, ids=[n for n, _ in MUST_BLOCK])
def test_credential_is_blocked(name: str, text: str) -> None:
    assert _blocks(_patterns(), text), f"the guard would have let {name} through"


@pytest.mark.parametrize("name,text", MUST_PASS, ids=[n for n, _ in MUST_PASS])
def test_harmless_text_is_not_blocked(name: str, text: str) -> None:
    assert not _blocks(_patterns(), text), f"the guard would wrongly block {name}"


def test_hook_greps_with_dash_e() -> None:
    """``grep -E "$PATTERNS"`` is not enough.

    The list starts with ``-----BEGIN``. Without ``-e``, grep reads it as a
    bundle of short options, exits 2, and the guard matches nothing while
    looking like it ran.
    """
    body = HOOK.read_text()
    for call in re.findall(r"grep [^|;\n]*\$PATTERNS[^|;\n]*", body):
        assert " -e " in call, f"pattern grep missing -e, so it fails open: {call.strip()}"


def test_hook_scans_untracked_files() -> None:
    """A leak arrives as a new file, which ``git diff`` does not report."""
    body = HOOK.read_text()
    assert "ls-files --others --exclude-standard" in body, (
        "the guard must scan untracked files; git diff alone misses a new file"
    )


def test_hook_allows_removing_a_credential() -> None:
    """Deleting a secret must never be blocked, or it cannot be cleaned up."""
    body = HOOK.read_text()
    assert "grep '^+'" in body, "the tracked scan must look at added lines only"


def test_hook_is_executable() -> None:
    assert HOOK.stat().st_mode & 0o111, "the Stop hook must be executable"


@pytest.mark.skipif(BASH is None, reason="needs bash")
def test_hook_is_syntactically_valid() -> None:
    subprocess.run(  # noqa: S603 - fixed argv, no shell
        [BASH, "-n", str(HOOK)], check=True, capture_output=True,
    )


def test_this_file_survives_its_own_guard() -> None:
    """The fixtures above are split into halves for a reason.

    Spelled as whole literals they would trip the guard, it would refuse to
    commit this file, and the tests proving the guard works could never be
    pushed. If someone joins them back up, this fails and says why.
    """
    patterns = _patterns()
    offenders = [
        f"{n}: {line.strip()}"
        for n, line in enumerate(HOOK.parent.parent.joinpath(
            "tests/test_auto_sync_guard.py").read_text().splitlines(), 1)
        if _blocks(patterns, line)
    ]
    assert not offenders, (
        "this file now contains a literal the guard blocks, so the hook will "
        "refuse to commit it; split it across a concatenation:\n"
        + "\n".join(offenders)
    )
