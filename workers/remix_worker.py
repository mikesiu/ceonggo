"""Ceonggo worker: put a new vocal (sung in OpenUtau) over a song's own band.

Runs inside ``runtime/sheetsage/python.exe``, which has onnxruntime-directml,
soundfile and numpy; FFmpeg comes from ``runtime/ffmpeg``. Two stages:

1. **separate** - UVR's MDX-Net "Inst HQ 3" takes the vocal out of the song,
   leaving the instrumental. Done here with onnxruntime directly rather than the
   audio-separator package, which needs numpy 2 and its own torch: the model is
   one ONNX graph between a short-time Fourier transform and its inverse.
   DirectML runs it on NVIDIA and AMD cards alike; the CPU is the fallback.
2. **mix** - FFmpeg lays the new vocal over the instrumental, shifted by the
   song's clock (YuE2 sings a few seconds off the score; see app/score/timing.py),
   and brings the result to -14 LUFS (measured, then one linear gain).

Job fields::

    song_audio, vocal, output_dir, model, ffmpeg,
    vocal_offset (seconds, default 0: + delays the vocal, - trims its start),
    vocal_gain_db (default 0), sample_rate (of the result, default 48000)
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _protocol as proto  # noqa: E402

#: Must equal EXPECTED_WORKER_VERSION in app/jobs/worker_proc.py.
WORKER_VERSION = 2

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

#: UVR-MDX-NET-Inst_HQ_3, from UVR's model_data.json (keyed by the file's hash):
#: compensate 1.022, mdx_dim_f_set 3072, mdx_dim_t_set 8, mdx_n_fft_scale_set 6144.
MODEL_RATE = 44100
N_FFT = 6144
HOP = 1024
DIM_F = 3072
DIM_T = 256
COMPENSATE = 1.022
#: -14 LUFS integrated. Two passes: one pass of loudnorm is its dynamic mode,
#: which landed a song at -13.3; measuring first and then applying one linear
#: gain lands on the target and leaves the mix's dynamics alone.
LOUDNESS = "loudnorm=I=-14:TP=-1.0:LRA=11"
MEASURED = ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset")


class _Stopped(InterruptedError):
    pass


def _check_cancel() -> None:
    if proto.cancelled():
        raise _Stopped("Cancelled")


class MdxNet:
    """The short-time Fourier transform pair MDX-Net was trained with
    (``torch.stft`` / ``istft`` with a periodic Hann window, centred, reflect
    padding) around one ONNX session, in numpy so torch need not load."""

    def __init__(self, model: str):
        import numpy as np
        import onnxruntime as ort

        self.np = np
        options = ort.SessionOptions()
        # DirectML cannot use memory patterns or parallel execution.
        options.enable_mem_pattern = False
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        wanted = [p for p in ("DmlExecutionProvider", "CPUExecutionProvider")
                  if p in ort.get_available_providers()]
        try:
            self.session = ort.InferenceSession(model, options, providers=wanted)
        except Exception as error:  # noqa: BLE001 - a driver that refuses DirectML still has a CPU
            print(f"DirectML unavailable ({error}); separating on the CPU", file=sys.stderr)
            self.session = ort.InferenceSession(model, options, providers=["CPUExecutionProvider"])
        self.provider = self.session.get_providers()[0]
        self.input = self.session.get_inputs()[0].name
        self.chunk = HOP * (DIM_T - 1)
        self.trim = N_FFT // 2
        self.window = (0.5 - 0.5 * np.cos(2 * np.pi * np.arange(N_FFT) / N_FFT)).astype(np.float32)
        self.frames = np.arange(N_FFT)[None, :] + HOP * np.arange(DIM_T)[:, None]

    def stft(self, wave):
        """(2, chunk) samples -> (4, DIM_F, DIM_T): left real, left imaginary, right real, right imaginary."""
        np = self.np
        padded = np.pad(wave, ((0, 0), (N_FFT // 2, N_FFT // 2)), mode="reflect")
        spec = np.fft.rfft(padded[:, self.frames] * self.window, axis=-1).transpose(0, 2, 1)
        out = np.stack([spec.real, spec.imag], axis=1).reshape(4, N_FFT // 2 + 1, DIM_T)
        return out[:, :DIM_F].astype(np.float32)

    def istft(self, spec):
        np = self.np
        full = np.zeros((4, N_FFT // 2 + 1, DIM_T), dtype=np.float32)
        full[:, :DIM_F] = spec
        full = full.reshape(2, 2, N_FFT // 2 + 1, DIM_T)
        frames = np.fft.irfft((full[:, 0] + 1j * full[:, 1]).transpose(0, 2, 1), n=N_FFT, axis=-1) * self.window
        length = N_FFT + HOP * (DIM_T - 1)
        out = np.zeros((2, length), dtype=np.float64)
        envelope = np.zeros(length, dtype=np.float64)
        for t in range(DIM_T):
            out[:, t * HOP:t * HOP + N_FFT] += frames[:, t]
            envelope[t * HOP:t * HOP + N_FFT] += self.window ** 2
        out /= np.where(envelope > 1e-11, envelope, 1.0)
        return out[:, N_FFT // 2:N_FFT // 2 + self.chunk]

    def instrumental(self, mix, progress):
        """(2, n) at 44.1 kHz -> the instrumental, same shape. Chunks overlap by
        N_FFT/2 each side and only their middles are kept, as UVR does."""
        np = self.np
        n = mix.shape[1]
        step = self.chunk - 2 * self.trim
        pad = step - n % step
        padded = np.concatenate([np.zeros((2, self.trim)), mix, np.zeros((2, pad + self.trim))], axis=1)
        starts = range(0, n + pad, step)
        parts = []
        for index, start in enumerate(starts):
            _check_cancel()
            spec = self.stft(padded[:, start:start + self.chunk])
            spec[:, :3] = 0                       # UVR zeroes the lowest bins before the model
            predicted = self.session.run(None, {self.input: spec[None]})[0][0]
            parts.append(self.istft(predicted)[:, self.trim:-self.trim])
            progress(index + 1, len(starts))
        return np.concatenate(parts, axis=1)[:, :n] * COMPENSATE


def ffmpeg(job: dict, args: list[str], *, seconds: float = 0.0, on_progress=None, level: str = "error") -> str:
    """Run FFmpeg and return what it wrote on stderr; with ``on_progress`` that is
    called with the seconds written so far."""
    command = [job["ffmpeg"], "-hide_banner", "-nostdin", "-y", "-v", level, *args]
    if on_progress is None:
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                                errors="replace", creationflags=NO_WINDOW)
        if result.returncode:
            raise RuntimeError(f"FFmpeg failed: {result.stderr.strip()[-600:]}")
        return result.stderr
    command[-1:-1] = ["-progress", "pipe:1", "-nostats"]
    proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
    try:
        for line in proc.stdout:
            if proto.cancelled():
                proc.kill()
                raise _Stopped("Cancelled")
            if line.startswith("out_time_us=") and line.strip()[12:].isdigit():
                on_progress(min(seconds, int(line.strip()[12:]) / 1e6))
    finally:
        error = proc.stderr.read()
        proc.wait()
    if proc.returncode:
        raise RuntimeError(f"FFmpeg failed: {error.strip()[-600:]}")
    return error


def measured_loudness(stderr: str) -> dict:
    """The JSON block loudnorm prints at the end of a measuring pass."""
    found = re.findall(r"\{[^{}]*\}", stderr)
    if not found:
        raise RuntimeError("FFmpeg did not report the loudness of the mix")
    values = json.loads(found[-1])
    return {key: values[key] for key in MEASURED}


def loudness(measured: dict | None) -> str:
    """loudnorm for the measuring pass (None), or the linear second pass."""
    if measured is None:
        return LOUDNESS + ":print_format=json"
    return (f"{LOUDNESS}:measured_I={measured['input_i']}:measured_TP={measured['input_tp']}"
            f":measured_LRA={measured['input_lra']}:measured_thresh={measured['input_thresh']}"
            f":offset={measured['target_offset']}:linear=true")


def mix_filter(offset: float, gain_db: float, measured: dict | None = None) -> str:
    """The FFmpeg graph: [0] instrumental, [1] vocal -> [out]."""
    vocal = ["aresample=44100", "aformat=channel_layouts=stereo"]
    if offset > 0:
        vocal.append(f"adelay=delays={round(offset * 1000)}:all=1")
    elif offset < 0:
        vocal += [f"atrim=start={-offset:.3f}", "asetpts=PTS-STARTPTS"]
    if gain_db:
        vocal.append(f"volume={gain_db:+.1f}dB")
    return (f"[1:a]{','.join(vocal)}[v];[0:a]aformat=channel_layouts=stereo[i];"
            f"[i][v]amix=inputs=2:duration=first:normalize=0,{loudness(measured)}[out]")


def run(job: dict) -> int:
    import numpy as np
    import soundfile as sf

    started = time.perf_counter()
    output = Path(job["output_dir"])
    output.mkdir(parents=True, exist_ok=True)
    for key in ("song_audio", "vocal", "model", "ffmpeg"):
        if not Path(job[key]).is_file():
            raise FileNotFoundError(f"{key} not found: {job[key]}")

    def stage(sid: str, label: str, completed, total, unit=None, status=None) -> None:
        proto.emit("stage", id=sid, label=label, completed=completed, total=total, unit=unit,
                   elapsed=round(time.perf_counter() - started, 2), status=status)

    stage("load", "Loading the separator", 0, None)
    separator = MdxNet(job["model"])
    proto.emit("hello", worker="remix", provider=separator.provider)
    stage("load", "Loading the separator", 1, 1, status="completed")

    # The model was trained at 44.1 kHz stereo; FFmpeg reads whatever the song is.
    decoded = output / "_mix_44k.wav"
    ffmpeg(job, ["-i", job["song_audio"], "-ac", "2", "-ar", str(MODEL_RATE), "-c:a", "pcm_f32le", str(decoded)])
    mix, _ = sf.read(str(decoded), dtype="float32", always_2d=True)
    decoded.unlink(missing_ok=True)
    stage("separate", "Extracting instrumental", 0, None, unit="chunks")
    inst = separator.instrumental(mix.T.astype(np.float64),
                                  lambda done, total: stage("separate", "Extracting instrumental", done, total,
                                                            unit="chunks"))
    instrumental = output / "instrumental.flac"
    sf.write(str(instrumental), np.clip(inst.T, -1.0, 1.0).astype(np.float32), MODEL_RATE, subtype="PCM_24")
    stage("separate", "Extracting instrumental", 1, 1, status="completed")

    _check_cancel()
    seconds = inst.shape[1] / MODEL_RATE
    rate = int(job.get("sample_rate") or 48000)
    audio = output / "audio.flac"
    offset, gain = float(job.get("vocal_offset") or 0), float(job.get("vocal_gain_db") or 0)
    inputs = ["-i", str(instrumental), "-i", job["vocal"]]
    total = round(2 * seconds, 1)                  # two passes over the song

    def mixing(done: float) -> None:
        stage("mix", "Mixing new vocals", round(done, 1), total, unit="seconds")

    mixing(0)
    report = ffmpeg(job, [*inputs, "-filter_complex", mix_filter(offset, gain), "-map", "[out]", "-f", "null", "-"],
                    seconds=seconds, on_progress=mixing, level="info")
    measured = measured_loudness(report)
    ffmpeg(job, [*inputs, "-filter_complex", mix_filter(offset, gain, measured), "-map", "[out]",
                 "-ar", str(rate), "-c:a", "flac", "-sample_fmt", "s32", "-bits_per_raw_sample", "24", str(audio)],
           seconds=seconds, on_progress=lambda done: mixing(seconds + done))
    stage("mix", "Mixing new vocals", total, total, unit="seconds", status="completed")

    from yue2_worker import write_peaks          # stdlib at import time; numpy only inside
    write_peaks(audio, output / "peaks.json")
    proto.emit("done", dir=str(output), audio=str(audio), instrumental=str(instrumental),
               provider=separator.provider, seconds=round(time.perf_counter() - started, 1))
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
