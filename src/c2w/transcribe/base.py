"""What a recogniser must provide, and what it gives back.

A protocol rather than one class, because the settings already offer six
engines -- local Whisper, faster-whisper, OpenAI's API, Azure, Google and AWS
-- and the choice is the customer's. Only the two local ones are implemented;
the rest raise a clear "not built" rather than pretending.

The shape of the result is deliberately the shape of the `transcripts` and
`transcript_segments` tables, so storing one is a loop and not a translation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Protocol

__all__ = [
    "EngineUnavailable",
    "Segment",
    "Transcriber",
    "TranscriptResult",
    "redact_digits",
]


class EngineUnavailable(RuntimeError):
    """The configured recogniser cannot run here.

    Separate from a transcription *failure*: this means the engine is missing
    or unimplemented, which is a configuration problem for a human, not
    something to retry.
    """


@dataclass(slots=True)
class Segment:
    """One stretch of speech."""

    seq: int
    start_ms: int
    end_ms: int
    text: str
    speaker: str | None = None
    confidence: float | None = None


@dataclass(slots=True)
class TranscriptResult:
    text: str
    language: str | None = None
    language_detected: str | None = None
    confidence: float | None = None
    duration_seconds: float | None = None
    segments: list[Segment] = field(default_factory=list)
    speaker_count: int | None = None
    engine: str = ""
    model: str = ""
    engine_detail: dict = field(default_factory=dict)


class Transcriber(Protocol):
    """A recogniser. Implementations must not raise for empty audio."""

    name: str

    async def transcribe(
        self,
        audio_path: str,
        *,
        language: str | None = None,
        word_timestamps: bool = True,
    ) -> TranscriptResult: ...


#: Seven or more digits in a row, allowing the spaces and dashes people speak
#: them with. Seven because a six-digit run is a date or an amount far more
#: often than it is an account number, and masking those would make a
#: transcript harder to read for no gain.
#
# Written to *end* on a digit: an earlier form allowed a trailing separator and
# swallowed the space after the number, turning "0044 ok" into "44ok".
_LONG_DIGITS = re.compile(r"(?<!\d)\d(?:[ \-.]?\d){6,}(?!\d)")


def redact_digits(text: str, *, keep_last: int = 2) -> str:
    """Mask long digit sequences, keeping a short tail.

    For `transcribe.redact_numbers`. A card or account number spoken aloud is
    the most sensitive thing in a sales call, and a transcript is far easier to
    copy out than audio. The tail is kept because "ending 41" is what makes a
    transcript still useful for finding the right call.
    """

    def mask(match: re.Match[str]) -> str:
        digits = re.sub(r"\D", "", match.group(0))
        if len(digits) <= keep_last:
            return match.group(0)
        return "*" * (len(digits) - keep_last) + digits[-keep_last:]

    return _LONG_DIGITS.sub(mask, text)
