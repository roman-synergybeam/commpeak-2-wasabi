"""Whisper, running on this server.

The default, and deliberately so: the alternative is posting recorded customer
calls to a third-party API, which for two brokers holding client conversations
is a decision somebody should make on purpose rather than inherit from a
default.

`faster-whisper` (CTranslate2) rather than the reference implementation --
several times quicker on CPU for the same model, which matters when the backlog
is 19 million recordings and there is no GPU.
"""

from __future__ import annotations

import asyncio
from typing import Any

from c2w.logging import get_logger
from c2w.transcribe.base import (
    EngineUnavailable,
    Segment,
    TranscriptResult,
    redact_digits,
)

__all__ = ["WhisperLocal"]

log = get_logger(__name__)

#: Model handles are loaded once per process and reused: loading is seconds and
#: several gigabytes for the larger sizes, and doing it per recording would
#: dominate the run.
_MODELS: dict[tuple[str, str], Any] = {}


def _language_code(setting: str | None) -> str | None:
    """Turn a settings label into a Whisper language code.

    The menu reads "es — Spanish (Latin America)" because that is what a person
    needs to choose between; Whisper wants "es". "auto-detect" becomes None,
    which is how you ask it to decide.
    """
    if not setting or setting.startswith("auto"):
        return None
    code = setting.split("—")[0].strip()
    # Whisper takes the base language, not a regional variant: pt-BR and pt-PT
    # are both "pt" to it, and passing the variant is silently ignored.
    return code.split("-")[0].lower() or None


class WhisperLocal:
    """Local Whisper via faster-whisper."""

    name = "faster-whisper-local"

    def __init__(
        self,
        model: str = "medium",
        *,
        device: str = "cpu",
        compute_type: str = "int8",
        redact: bool = False,
        beam_size: int = 1,
    ) -> None:
        self.model_name = model
        self.device = device
        self.compute_type = compute_type
        self.redact = redact
        self.beam_size = beam_size

    def _model(self) -> Any:
        key = (self.model_name, self.compute_type)
        if key in _MODELS:
            return _MODELS[key]
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise EngineUnavailable(
                "faster-whisper is not installed on this server"
            ) from exc
        log.info(
            "transcribe.loading_model", model=self.model_name, compute=self.compute_type
        )
        try:
            _MODELS[key] = WhisperModel(
                self.model_name, device=self.device, compute_type=self.compute_type
            )
        except Exception as exc:
            # A missing model download, or no room for it, is a configuration
            # problem rather than a per-recording failure.
            raise EngineUnavailable(
                f"could not load the {self.model_name} model: {exc}"
            ) from exc
        return _MODELS[key]

    async def transcribe(
        self,
        audio_path: str,
        *,
        language: str | None = None,
        word_timestamps: bool = True,
    ) -> TranscriptResult:
        """Recognise one file.

        Runs in a worker thread: CTranslate2 is CPU-bound C++ and would block
        the event loop for the whole recording otherwise, stalling every other
        request in the process.
        """
        return await asyncio.to_thread(
            self._transcribe_blocking, audio_path, _language_code(language), word_timestamps
        )

    def _transcribe_blocking(
        self, audio_path: str, language: str | None, word_timestamps: bool
    ) -> TranscriptResult:
        model = self._model()
        segments, info = model.transcribe(
            audio_path,
            language=language,
            beam_size=self.beam_size,
            word_timestamps=word_timestamps,
            # Call audio is full of silence between turns; skipping it is both
            # faster and stops Whisper inventing words to fill the gap.
            vad_filter=True,
        )

        out: list[Segment] = []
        parts: list[str] = []
        total_logprob = 0.0
        for index, seg in enumerate(segments):
            text = seg.text.strip()
            if not text:
                continue
            if self.redact:
                text = redact_digits(text)
            parts.append(text)
            total_logprob += getattr(seg, "avg_logprob", 0.0) or 0.0
            out.append(
                Segment(
                    seq=index,
                    start_ms=int((seg.start or 0.0) * 1000),
                    end_ms=int((seg.end or 0.0) * 1000),
                    text=text,
                    confidence=_confidence(getattr(seg, "avg_logprob", None)),
                )
            )

        return TranscriptResult(
            text=" ".join(parts),
            language=language,
            language_detected=getattr(info, "language", None),
            confidence=_confidence(total_logprob / len(out)) if out else None,
            duration_seconds=getattr(info, "duration", None),
            segments=out,
            engine=self.name,
            model=self.model_name,
            engine_detail={
                "language_probability": round(
                    float(getattr(info, "language_probability", 0.0) or 0.0), 3
                ),
                "vad_filter": True,
                "beam_size": self.beam_size,
            },
        )


def _confidence(avg_logprob: float | None) -> float | None:
    """A 0..1 confidence from Whisper's average log probability.

    Whisper reports a log probability, which is not a number anybody can read.
    Exponentiating gives something monotonic and bounded that can be compared
    between segments -- it is not a calibrated probability and is not presented
    as one.
    """
    if avg_logprob is None:
        return None
    import math

    return round(min(1.0, max(0.0, math.exp(avg_logprob))), 4)
