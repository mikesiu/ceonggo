"""Ceonggo worker: split any recording into its vocal and its instrumental.

Runs inside ``runtime/sheetsage/python.exe`` with the separator Replace vocal
uses: UVR's MDX-Net "Inst HQ 3" on onnxruntime (``MdxNet`` in remix_worker.py,
which says why not the audio-separator package). The model predicts the
instrumental; the vocal is what is left when that is taken from the mix, which
is how audio-separator makes this model's second stem.

FFmpeg reads the recording (MP3, WAV, FLAC ...) at the model's 44.1 kHz. Both
stems are written as 24-bit FLAC, each to a ``.part`` file renamed only once
both are complete, so a failed run never leaves half a file under the name.

Job fields::

    audio, vocals, instrumental (full paths of the two results), model, ffmpeg
"""
from __future__ import annotations

import sys
import tempfile
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _protocol as proto  # noqa: E402
from remix_worker import MODEL_RATE, MdxNet, ffmpeg  # noqa: E402  (stdlib at import time; numpy only inside)

#: Must equal EXPECTED_WORKER_VERSION in app/jobs/worker_proc.py.
WORKER_VERSION = 2


def write_stems(stems: list[tuple[object, Path]]) -> None:
    """Write (samples (2, n), path) pairs as FLAC: all of them, or none."""
    import numpy as np
    import soundfile as sf

    parts = [path.with_name(path.name + ".part") for _, path in stems]
    try:
        for (samples, _), part in zip(stems, parts):
            sf.write(str(part), np.clip(samples.T, -1.0, 1.0).astype(np.float32), MODEL_RATE,
                     format="FLAC", subtype="PCM_24")
        for (_, path), part in zip(stems, parts):
            part.replace(path)
    finally:
        for part in parts:
            part.unlink(missing_ok=True)


def run(job: dict) -> int:
    import numpy as np
    import soundfile as sf

    started = time.perf_counter()
    for key in ("audio", "model", "ffmpeg"):
        if not Path(job[key]).is_file():
            raise FileNotFoundError(f"{key} not found: {job[key]}")
    vocals, instrumental = Path(job["vocals"]), Path(job["instrumental"])
    vocals.parent.mkdir(parents=True, exist_ok=True)
    instrumental.parent.mkdir(parents=True, exist_ok=True)

    def stage(sid: str, label: str, completed, total, unit=None, status=None) -> None:
        proto.emit("stage", id=sid, label=label, completed=completed, total=total, unit=unit,
                   elapsed=round(time.perf_counter() - started, 2), status=status)

    stage("load", "Loading the separator", 0, None)
    separator = MdxNet(job["model"])
    proto.emit("hello", worker="extract", provider=separator.provider)
    stage("load", "Loading the separator", 1, 1, status="completed")

    # The model was trained at 44.1 kHz stereo; FFmpeg reads whatever the file is
    # (-vn: an MP3's cover picture is not audio).
    stage("read", "Reading the recording", 0, None)
    with tempfile.TemporaryDirectory(prefix="extract-") as scratch:
        decoded = Path(scratch) / "mix_44k.wav"
        ffmpeg(job, ["-i", job["audio"], "-vn", "-ac", "2", "-ar", str(MODEL_RATE), "-c:a", "pcm_f32le",
                     str(decoded)])
        mix, _ = sf.read(str(decoded), dtype="float32", always_2d=True)
    mix = mix.T.astype(np.float64)
    stage("read", "Reading the recording", 1, 1, status="completed")

    stage("separate", "Separating the vocals", 0, None, unit="chunks")
    inst = separator.instrumental(mix, lambda done, total: stage("separate", "Separating the vocals", done, total,
                                                                 unit="chunks"))
    stage("separate", "Separating the vocals", 1, 1, status="completed")

    if proto.cancelled():
        raise InterruptedError("Cancelled")
    stage("save", "Saving the vocals and instrumental", 0, None)
    write_stems([(mix - inst, vocals), (inst, instrumental)])
    stage("save", "Saving the vocals and instrumental", 1, 1, status="completed")
    proto.emit("done", vocals=str(vocals), instrumental=str(instrumental), provider=separator.provider,
               seconds=round(time.perf_counter() - started, 1))
    return 0


def main() -> int:
    proto.claim_stdout()
    try:
        return run(proto.read_job())
    except BaseException as exc:  # noqa: BLE001 - report everything to the window
        message = f"{type(exc).__name__}: {exc}"
        kind = "cancelled" if isinstance(exc, (InterruptedError, KeyboardInterrupt)) or proto.cancelled() \
            else proto.classify_error(message)
        traceback.print_exc()
        proto.emit("error", kind=kind, message=message)
        return 2 if kind == "cancelled" else 1


if __name__ == "__main__":
    raise SystemExit(main())
