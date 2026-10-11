"""c2w transcription agent -- runs Whisper on this PC's GPU for the c2w console.

It asks the console for a few recordings at a time, downloads each one into
memory from a link that works for that one file for twenty minutes, checks
whether anybody actually speaks in it, transcribes it if so, and sends the
text back. The audio is never written to this PC's disk and nothing here can
reach the rest of the archive.

    python c2w_agent.py           run until Ctrl+C
    python c2w_agent.py --check   prove the GPU, the model and the token work

Settings are in %LOCALAPPDATA%\\c2w-agent\\config.json, written by install.ps1.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import platform
import queue
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

VERSION = "1.0"
HOME = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "c2w-agent"
CONFIG = HOME / "config.json"
LOG = HOME / "agent.log"
SAMPLE_RATE = 16_000
BATCH = 16          # recordings per claim
PREFETCH = 2        # claims held downloaded ahead of the GPU
DOWNLOADS = 8       # files downloaded at once; each is small and latency-bound
IDLE_SLEEP = 60     # seconds to wait when the console has nothing to do


def _add_cuda_dlls() -> None:
    """Make the CUDA libraries pip installed visible to CTranslate2 on Windows.

    The nvidia-* wheels put their DLLs under site-packages/nvidia/*/bin, which
    is not on the DLL search path, and without them the GPU load fails with a
    missing cublas64_12.dll.
    """
    if os.name != "nt":
        return
    base = Path(sys.prefix) / "Lib" / "site-packages" / "nvidia"
    for sub in ("cublas", "cudnn", "cuda_nvrtc", "cuda_runtime"):
        path = base / sub / "bin"
        if path.is_dir():
            os.add_dll_directory(str(path))
            os.environ["PATH"] = str(path) + os.pathsep + os.environ.get("PATH", "")


_add_cuda_dlls()

import requests  # noqa: E402


def say(message: str) -> None:
    line = f"[{datetime.now():%H:%M:%S}] {message}"
    print(line, flush=True)
    try:
        with LOG.open("a", encoding="utf-8") as fh:
            fh.write(f"{datetime.now():%Y-%m-%d} {line}\n")
    except OSError:
        pass


class Console:
    """The c2w console's agent API."""

    def __init__(self, server: str, token: str) -> None:
        self.base = server.rstrip("/") + "/api/agent/v1"
        self.http = requests.Session()
        self.http.headers["Authorization"] = f"Bearer {token}"
        self.http.headers["User-Agent"] = f"c2w-agent/{VERSION}"

    def _post(self, path: str, body: dict) -> dict:
        response = self.http.post(self.base + path, json=body, timeout=60)
        if response.status_code == 401:
            raise SystemExit("The console refused this agent's token. Was it revoked?")
        response.raise_for_status()
        return response.json()

    def claim(self, n: int, device: str) -> dict:
        return self._post(
            "/claim",
            {"max": n, "agent": {"version": VERSION, "device": device, "host": platform.node()}},
        )

    def result(self, job_id: str, body: dict) -> str:
        response = self.http.post(f"{self.base}/jobs/{job_id}/result", json=body, timeout=60)
        if response.status_code == 409:
            return "expired"
        response.raise_for_status()
        return response.json()["outcome"]

    def give_back(self, job_id: str, reason: str, *, retry: bool = True) -> None:
        try:
            self.http.post(
                f"{self.base}/jobs/{job_id}/fail",
                json={"reason": reason[:400], "retry": retry},
                timeout=30,
            )
        except requests.RequestException:
            pass  # the lease expires on its own

    def progress(self) -> list[dict]:
        response = self.http.get(self.base + "/progress", timeout=60)
        response.raise_for_status()
        return response.json()["progress"]


class Recogniser:
    def __init__(self, model_name: str, use_cpu: bool = False) -> None:
        from faster_whisper import BatchedInferencePipeline, WhisperModel

        self.model_name = model_name
        if use_cpu:
            self.device = f"CPU ({os.cpu_count()} threads)"
            model = WhisperModel(model_name, device="cpu", compute_type="int8")
        else:
            model = WhisperModel(model_name, device="cuda", compute_type="float16")
            self.device = _gpu_name()
        self.model = model
        self.batched = BatchedInferencePipeline(model=model)

    def run(self, audio, job: dict) -> dict:
        from faster_whisper.vad import get_speech_timestamps

        started = time.perf_counter()
        duration = len(audio) / SAMPLE_RATE
        spans = get_speech_timestamps(audio, sampling_rate=SAMPLE_RATE)
        speech = round(sum(s["end"] - s["start"] for s in spans) / SAMPLE_RATE, 2)
        body = {
            "duration_seconds": round(duration, 2),
            "speech_seconds": speech,
            "language": job.get("language"),
            "model": self.model_name,
            "device": self.device,
            "segments": [],
        }
        # Hold music, ringing and silence end here, in a fraction of a second.
        # The console makes the final call on what to keep; this only avoids
        # running the model on what it will certainly throw away.
        if speech < (job.get("min_speech_seconds") or 0) or duration < (
            job.get("only_longer_than") or 0
        ):
            body["seconds_taken"] = round(time.perf_counter() - started, 2)
            return body

        segments, info = self.batched.transcribe(
            audio,
            language=job.get("language"),
            word_timestamps=bool(job.get("word_timestamps")),
            batch_size=16,
            vad_filter=True,
        )
        import math

        body["segments"] = [
            {
                "start": round(s.start or 0.0, 2),
                "end": round(s.end or 0.0, 2),
                "text": s.text.strip(),
                "confidence": round(min(1.0, math.exp(s.avg_logprob)), 4)
                if s.avg_logprob is not None
                else None,
            }
            for s in segments
            if s.text.strip()
        ]
        body["language_detected"] = info.language
        body["language_probability"] = round(float(info.language_probability or 0), 3)
        body["seconds_taken"] = round(time.perf_counter() - started, 2)
        return body


def _gpu_name() -> str:
    try:
        import subprocess

        # On PATH wherever the NVIDIA driver is installed.
        query = ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"]
        out = subprocess.run(query, capture_output=True, text=True, timeout=10)  # noqa: S603
        return out.stdout.strip().splitlines()[0]
    except Exception:
        return "CUDA GPU"


def fetcher(console: Console, device: str, out: queue.Queue, stop: threading.Event) -> None:
    """Claim and download ahead of the GPU, so it never waits on the network."""
    from faster_whisper.audio import decode_audio

    download = requests.Session()
    download.mount("https://", requests.adapters.HTTPAdapter(pool_maxsize=DOWNLOADS))
    backoff = 5
    while not stop.is_set():
        try:
            claimed = console.claim(BATCH, device)
        except SystemExit as exc:
            out.put(("fatal", str(exc)))
            return
        except requests.RequestException as exc:
            say(f"console unreachable ({type(exc).__name__}); retrying in {backoff}s")
            stop.wait(backoff)
            backoff = min(backoff * 2, 300)
            continue
        backoff = 5
        out.put(("progress", claimed.get("progress") or []))
        jobs = claimed.get("jobs") or []
        if not jobs:
            say("nothing to transcribe right now; checking again in a minute")
            stop.wait(IDLE_SLEEP)
            continue
        def fetch(job: dict):
            if stop.is_set():
                console.give_back(job["job_id"], "agent stopped")
                return
            try:
                response = download.get(job["audio_url"], timeout=120)
                response.raise_for_status()
                audio = decode_audio(io.BytesIO(response.content), sampling_rate=SAMPLE_RATE)
            except Exception as exc:
                console.give_back(job["job_id"], f"download failed: {type(exc).__name__}: {exc}")
                say(f"download failed, handed back: {type(exc).__name__}")
                return
            out.put(("job", (job, audio)))

        # Several at once: a call recording is under a megabyte, so each
        # download is mostly waiting on the round trip, not on bandwidth.
        with ThreadPoolExecutor(max_workers=DOWNLOADS) as pool:
            list(pool.map(fetch, jobs))


def show_progress(rows: list[dict]) -> None:
    for row in rows:
        eta = row.get("eta_hours")
        when = "no recent progress" if eta is None else f"about {_eta_words(eta)} to go"
        say(
            f"  {row['organisation']}: {row['processed']:,} of {row['archived']:,} done, "
            f"{row['remaining']:,} left, {row['per_hour']:,.0f}/hour, {when}"
        )


def _eta_words(hours: float) -> str:
    if hours < 48:
        return f"{hours:.0f} hours"
    if hours < 24 * 60:
        return f"{hours / 24:.0f} days"
    return f"{hours / 24 / 30:.1f} months"


def load_config() -> dict:
    if not CONFIG.exists():
        raise SystemExit(f"No {CONFIG}. Run install.ps1 first.")
    # utf-8-sig: Windows PowerShell 5.1's Set-Content -Encoding UTF8 writes a
    # byte-order mark, which plain utf-8 json refuses.
    return json.loads(CONFIG.read_text(encoding="utf-8-sig"))


def check(config: dict, use_cpu: bool) -> int:
    console = Console(config["server"], config["token"])
    say(f"console: {config['server']}")
    for row in console.progress():
        say(f"  token accepted; {row['organisation']} has {row['remaining']:,} to go")
    say(f"loading the {config.get('model', 'turbo')} model (the first time downloads ~1.6 GB)")
    recogniser = Recogniser(config.get("model", "turbo"), use_cpu=use_cpu)
    import numpy as np

    tone = (0.1 * np.sin(np.linspace(0, 440 * 2 * np.pi * 3, SAMPLE_RATE * 3))).astype("float32")
    # A generator: nothing runs on the GPU until it is iterated.
    segments, _ = recogniser.model.transcribe(tone, vad_filter=False)
    list(segments)
    say(f"OK: {recogniser.device} runs the model")
    return 0


def run(config: dict, use_cpu: bool, limit: int = 0) -> int:
    console = Console(config["server"], config["token"])
    say(f"c2w agent {VERSION} on {platform.node()}, console {config['server']}")
    say(f"loading the {config.get('model', 'turbo')} model")
    recogniser = Recogniser(config.get("model", "turbo"), use_cpu=use_cpu)
    say(f"ready on {recogniser.device}. Ctrl+C to stop.")

    work: queue.Queue = queue.Queue(maxsize=BATCH * PREFETCH)
    stop = threading.Event()
    thread = threading.Thread(
        target=fetcher, args=(console, recogniser.device, work, stop), daemon=True
    )
    thread.start()

    counts = {"stored": 0, "skipped": 0, "expired": 0, "failed": 0}
    started = time.monotonic()
    last_progress = 0.0
    try:
        while True:
            kind, item = work.get()
            if kind == "fatal":
                say(item)
                return 1
            if kind == "progress":
                if time.monotonic() - last_progress > 600 or last_progress == 0:
                    say("progress (all transcribers together):")
                    show_progress(item)
                    last_progress = time.monotonic()
                continue
            job, audio = item
            try:
                body = recogniser.run(audio, job)
            except Exception as exc:
                memory = "out of memory" in str(exc).lower()
                console.give_back(
                    job["job_id"], f"{type(exc).__name__}: {exc}", retry=memory
                )
                counts["failed"] += 1
                say(f"recognition failed: {type(exc).__name__}: {str(exc)[:120]}")
                continue
            try:
                outcome = console.result(job["job_id"], body)
            except requests.RequestException as exc:
                say(f"could not send a result ({type(exc).__name__}); it will be redone")
                continue
            counts[outcome] = counts.get(outcome, 0) + 1
            done = sum(counts.values())
            rate = done / max(1e-6, (time.monotonic() - started) / 3600)
            words = len(body["segments"])
            say(
                f"{outcome:7} {body['duration_seconds']:6.0f}s audio, "
                f"{body['speech_seconds']:5.0f}s speech, {words:3} segments, "
                f"{body.get('seconds_taken', 0):5.1f}s   "
                f"[this PC: {done:,} done, {rate:,.0f}/hour]"
            )
            if limit and done >= limit:
                say(f"stopping after {done} recordings, as asked")
                return 0
    except KeyboardInterrupt:
        say("stopping; handing back anything not yet transcribed")
    finally:
        stop.set()
        while True:
            try:
                kind, item = work.get_nowait()
            except queue.Empty:
                break
            if kind == "job":
                console.give_back(item[0]["job_id"], "agent stopped")
    return 0


def main() -> int:
    global BATCH, DOWNLOADS
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="test the GPU, model and token")
    parser.add_argument("--cpu", action="store_true", help="use the CPU instead of the GPU")
    parser.add_argument("--batch", type=int, default=BATCH, help="recordings per claim")
    parser.add_argument("--downloads", type=int, default=DOWNLOADS, help="parallel downloads")
    parser.add_argument("--limit", type=int, default=0, help="stop after this many (testing)")
    args = parser.parse_args()
    BATCH, DOWNLOADS = max(1, min(args.batch, 32)), max(1, min(args.downloads, 32))
    HOME.mkdir(parents=True, exist_ok=True)
    config = load_config()
    return check(config, args.cpu) if args.check else run(config, args.cpu, args.limit)


if __name__ == "__main__":
    sys.exit(main())
