"""Local speech-to-text integration (Phase 5B-1).

Free and offline only. Engines are auto-detected when ALREADY installed:
``faster-whisper`` (local model dir only) and ``vosk`` (model dir on disk).
NOTHING is ever downloaded, no paid APIs, no keys, no audio leaves the
machine through this module. When no usable engine is present every call
fails CLOSED with ``stt-unavailable`` so the caller can say so honestly and
fall back to browser recognition or typing.

Engine objects are injectable (tests use fakes). Detection never imports
anything at module import time; every third-party import is lazy and guarded.
The faster-whisper/vosk adapters below run only where those packages (and a
local model) exist — this environment has neither, so they are covered by
contract tests with fakes, not live runs.
"""
from __future__ import annotations

import gc
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger("interview_stt")

SUPPORTED_LANGS = ("ru", "en")
#: Refuse uploads larger than this (a few minutes of compressed audio).
MAX_AUDIO_BYTES = 5 * 1024 * 1024

ERR_UNAVAILABLE = "stt-unavailable"
ERR_LANG = "lang-unsupported"
ERR_EMPTY = "empty-audio"
ERR_TOO_LARGE = "audio-too-large"
ERR_UNREADABLE = "audio-unreadable"
ERR_ENGINE = "engine-error"


def _fail(error: str, retryable: bool = False, detail: str = "") -> dict[str, Any]:
    return {"ok": False, "error": error, "retryable": retryable, "detail": detail}


def validate_audio(audio: bytes, lang: str) -> dict[str, Any] | None:
    """Return an error dict for bad input, or None when the input is usable."""
    if lang not in SUPPORTED_LANGS:
        return _fail(ERR_LANG, detail=f"lang must be one of {list(SUPPORTED_LANGS)}")
    if not audio:
        return _fail(ERR_EMPTY, detail="no audio bytes received")
    if len(audio) > MAX_AUDIO_BYTES:
        return _fail(ERR_TOO_LARGE,
                     detail=f"{len(audio)} bytes exceeds {MAX_AUDIO_BYTES}")
    return None


class LocalEngine:
    """Minimal engine protocol: name, langs, transcribe(bytes, lang) -> str."""

    name = "base"
    langs: tuple[str, ...] = ()

    def transcribe(self, audio: bytes, lang: str) -> str:
        raise NotImplementedError


def _suffix_for_audio(audio: bytes) -> str:
    """Container suffix for the decoder: WAV and M4A passthrough, else WebM.

    Browsers send EBML/WebM (opus); phone recorders send MP4/M4A (AAC) —
    both verified against faster-whisper/PyAV locally. PyAV probes content,
    the suffix only selects the demuxer hint.
    """
    if audio[:4] == b"RIFF":
        return ".wav"
    if len(audio) > 8 and audio[4:8] == b"ftyp":
        return ".m4a"
    return ".webm"


def _resolve_device() -> tuple[str, str]:
    """(device, compute_type) from STT_DEVICE=auto|cpu|cuda (default auto)."""
    want = os.getenv("STT_DEVICE", "auto").strip().lower()
    if want == "cuda":
        return "cuda", "float16"
    if want == "cpu":
        return "cpu", "int8"
    return "auto", ""


#: Resident models keyed by (model_dir, device): live use transcribes every
#: few seconds, and reloading ~0.5 GB per call would dominate latency.
#: Bounded (2 entries); `clear_model_cache()` resets it for tests.
_MODEL_CACHE: dict[tuple[str, str], Any] = {}


def clear_model_cache() -> None:
    _MODEL_CACHE.clear()


def warmup(model_dir: str | None = None) -> dict[str, Any]:
    """Managed model warmup for interview prep (no mic, no recording).

    Loads the resident model (CUDA first, CPU fallback) and runs one short
    silence transcribe to trigger kernel autotune, so the first live chunk
    does not pay cold-start costs. Synthesizes silence in memory — no audio
    is captured, read, or stored. Never raises for engine problems.
    """
    import time as _time

    started = _time.monotonic()
    try:
        engine = _FasterWhisperEngine(model_dir)
        engine._load()
    except Exception as exc:
        return {"ok": False, "error": "stt-unavailable", "retryable": False,
                "detail": f"warmup load failed: {type(exc).__name__}",
                "load_s": round(_time.monotonic() - started, 1)}
    load_s = round(_time.monotonic() - started, 1)
    try:
        # Full decode path (VAD off) so CUDA/CPU kernels autotune now, not on
        # the first live chunk. Silence carries no information either way.
        engine.transcribe(_silence_wav_bytes(), "en", vad=False)
    except Exception as exc:
        return {"ok": False, "error": "engine-error", "retryable": False,
                "detail": f"warmup inference failed: {type(exc).__name__}",
                "device": engine.last_device or "unknown", "load_s": load_s}
    return {"ok": True, "engine": "faster-whisper",
            "device": engine.last_device or "unknown", "load_s": load_s,
            "warmup_s": round(_time.monotonic() - started, 1)}


def _silence_wav_bytes(seconds: float = 1.0, rate: int = 16000) -> bytes:
    """In-memory silent WAV for warmup inference (never touches disk/mic)."""
    import io as _io
    import wave as _wave

    buf = _io.BytesIO()
    with _wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(b"\x00\x00" * int(rate * seconds))
    return buf.getvalue()


class _FasterWhisperEngine(LocalEngine):
    """faster-whisper against a LOCAL model directory only.

    ``local_files_only`` is forced: the library downloads the model on first
    use by default, and silent downloads are not acceptable here. Without
    STT_MODEL_DIR (or a models/faster-whisper-* dir) the engine reports
    itself unusable instead of fetching anything.
    """

    name = "faster-whisper"
    langs = ("ru", "en")

    def __init__(self, model_dir: str | None = None) -> None:
        self._model_dir = model_dir or os.getenv("STT_MODEL_DIR", "").strip()
        self.last_device = ""

    def _load(self):
        from faster_whisper import WhisperModel  # lazy: optional dependency

        if not self._model_dir or not Path(self._model_dir).is_dir():
            raise RuntimeError("local faster-whisper model dir is not configured")
        device, compute = _resolve_device()
        if device == "auto":
            key = (self._model_dir, "cuda")
            if key in _MODEL_CACHE:
                self.last_device = "cuda"
                return _MODEL_CACHE[key]
            try:
                model = WhisperModel(self._model_dir, device="cuda",
                                     compute_type="float16", local_files_only=True)
                self.last_device = "cuda"
                _MODEL_CACHE[key] = model
                while len(_MODEL_CACHE) > 2:
                    _MODEL_CACHE.pop(next(iter(_MODEL_CACHE)))
                return model
            except Exception as exc:
                logger.info("CUDA load failed (%s), falling back to CPU",
                            type(exc).__name__)
            device, compute = "cpu", "int8"
            self.last_device = "cpu"
        else:
            self.last_device = device
            compute = compute or "int8"
        key = (self._model_dir, self.last_device)
        if key not in _MODEL_CACHE:
            _MODEL_CACHE[key] = WhisperModel(
                self._model_dir, device=self.last_device,
                compute_type=compute, local_files_only=True)
            while len(_MODEL_CACHE) > 2:
                _MODEL_CACHE.pop(next(iter(_MODEL_CACHE)))
        return _MODEL_CACHE[key]

    def transcribe(self, audio: bytes, lang: str, vad: bool = True) -> str:
        model = self._load()
        suffix = _suffix_for_audio(audio)
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(audio)
            tmp_path = tmp.name
        try:
            # VAD first (bundled silero, offline): silent chunks resolve in a
            # fraction of a second instead of paying a full no-speech decode,
            # and silence hallucinations ("subtitle editor…") never form.
            segments, _info = model.transcribe(tmp_path, language=lang,
                                               beam_size=5,
                                               condition_on_previous_text=False,
                                               vad_filter=vad)
            text = " ".join(s.text.strip() for s in segments).strip()
            # Release the decoder BEFORE unlink: PyAV may still hold the file
            # (Windows refuses unlink on open handles, leaking Temp on every
            # chunk of a live pipeline). GC + a few retries close that gap.
            del segments
            gc.collect()
            return text
        finally:
            for _ in range(5):
                try:
                    os.unlink(tmp_path)
                    break
                except OSError:
                    time.sleep(0.2)


class _VoskEngine(LocalEngine):
    """Vosk offline recognizer. Model dir must already exist; the 16 kHz mono
    WAV path is validated with the stdlib ``wave`` module. Anything else is
    reported unreadable rather than transcoded (no ffmpeg/numpy here)."""

    name = "vosk"
    langs = ("ru", "en")

    def __init__(self, model_dir: str | None = None) -> None:
        self._model_dir = model_dir or os.getenv("VOSK_MODEL_DIR", "").strip()

    def _resolve_model(self, lang: str) -> str:
        if self._model_dir and Path(self._model_dir).is_dir():
            return self._model_dir
        for cand in sorted(Path("models").glob("vosk-model-*")):
            if cand.is_dir() and (lang in cand.name or "small" in cand.name):
                return str(cand)
        raise RuntimeError("no vosk model directory on disk (not downloading)")

    def transcribe(self, audio: bytes, lang: str) -> str:
        import json as _json
        import wave as _wave
        from vosk import KaldiRecognizer, Model  # lazy: optional dependency

        import io as _io

        try:
            with _wave.open(_io.BytesIO(audio)) as wav:
                if wav.getnchannels() != 1 or wav.getsampwidth() != 2 \
                        or wav.getframerate() != 16000:
                    raise ValueError("vosk needs 16kHz mono 16-bit WAV")
                frames = wav.readframes(wav.getnframes())
        except Exception as exc:
            raise ValueError(f"unreadable audio for vosk: {exc}") from exc
        rec = KaldiRecognizer(Model(self._resolve_model(lang)), 16000)
        rec.AcceptWaveform(frames)
        return _json.loads(rec.FinalResult()).get("text", "").strip()


def detect_engines(
    import_fn: Callable[[str], Any] | None = None,
    model_dir: str | None = None,
) -> dict[str, Any]:
    """Describe locally usable STT engines. Never downloads, never raises.

    Returns {"available": bool, "engines": [{"name", "langs", "usable",
    "detail"}]}. ``import_fn`` exists so tests can simulate installed engines
    without installing anything.
    """
    if import_fn is None:
        import importlib as _il

        def import_fn(name: str) -> Any:
            return _il.import_module(name)

    engines: list[dict[str, Any]] = []

    try:
        import_fn("faster_whisper")
        fw = _FasterWhisperEngine(model_dir)
        usable = bool(fw._model_dir and Path(fw._model_dir).is_dir())
        engines.append({"name": fw.name, "langs": list(fw.langs),
                        "usable": usable,
                        "detail": ("local model ready" if usable
                                   else "installed but no local model dir — "
                                        "set STT_MODEL_DIR (no downloads)")})
    except Exception as exc:
        logger.debug("faster-whisper unavailable: %s", type(exc).__name__)

    try:
        import_fn("vosk")
        vosk = _VoskEngine(model_dir)
        try:
            resolved = vosk._resolve_model("ru")
            usable, detail = True, f"model at {resolved}"
        except Exception:
            usable, detail = False, ("installed but no model on disk — set "
                                     "VOSK_MODEL_DIR (no downloads)")
        engines.append({"name": vosk.name, "langs": list(vosk.langs),
                        "usable": usable, "detail": detail})
    except Exception as exc:
        logger.debug("vosk unavailable: %s", type(exc).__name__)

    usable_any = any(e["usable"] for e in engines)
    return {"available": usable_any, "engines": engines,
            "reason": "" if usable_any else ERR_UNAVAILABLE}


def _build_engine(name: str, model_dir: str | None = None) -> LocalEngine:
    if name == "faster-whisper":
        return _FasterWhisperEngine(model_dir)
    if name == "vosk":
        return _VoskEngine(model_dir)
    raise ValueError(f"unknown engine {name!r}")


def transcribe_audio(audio: bytes, lang: str, content_type: str | None = None,
                     engine: str | None = None,
                     engine_obj: LocalEngine | None = None,
                     detect_fn: Callable[..., dict[str, Any]] | None = None,
                     ) -> dict[str, Any]:
    """Transcribe audio bytes. Never raises for input/engine failures.

    ``engine_obj`` injects a ready engine (tests). Otherwise the first usable
    detected engine for ``lang`` is used, or ``engine`` names one explicitly.
    ``content_type`` is informational only (browsers send audio/webm).
    """
    bad = validate_audio(audio, lang)
    if bad:
        return bad
    _detect = detect_fn or detect_engines
    try:
        picked: LocalEngine | None = engine_obj
        if picked is None:
            info = _detect()
            cands = [e for e in info.get("engines", [])
                     if e.get("usable") and lang in e.get("langs", [])]
            if engine:
                cands = [e for e in cands if e["name"] == engine]
                if not cands:
                    return _fail(ERR_UNAVAILABLE, detail=f"engine {engine!r} not usable")
            if not cands:
                return _fail(ERR_UNAVAILABLE,
                             detail="no local STT engine usable; "
                                    "use browser voice input or type the question")
            picked = _build_engine(cands[0]["name"])
        text = (picked.transcribe(audio, lang) or "").strip()
    except ValueError as exc:
        logger.warning("interview STT input rejected: %s", exc)
        return _fail(ERR_UNREADABLE, detail=str(exc)[:200])
    except Exception as exc:  # noqa: BLE001 - mapped, not raised
        logger.warning("interview STT engine failed: %s", type(exc).__name__)
        retryable = isinstance(exc, TimeoutError)
        return _fail(ERR_ENGINE, retryable=retryable,
                     detail=type(exc).__name__)
    if not text:
        return _fail(ERR_UNREADABLE, detail="engine returned empty transcript")
    return {"ok": True, "text": text, "lang": lang, "engine": picked.name,
            "device": getattr(picked, "last_device", "") or "unknown",
            "audio_bytes": len(audio),
            "content_type": (content_type or "").split(";")[0].strip() or "unknown"}


__all__ = ["SUPPORTED_LANGS", "MAX_AUDIO_BYTES", "clear_model_cache",
           "detect_engines", "transcribe_audio", "validate_audio", "warmup",
           "LocalEngine"]
