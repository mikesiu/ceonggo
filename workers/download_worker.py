"""Ceonggo worker: the audio of one link (YouTube and the other sites yt-dlp knows), as FLAC or MP3.

Runs inside ``runtime/sheetsage/python.exe``, where Setup installs yt-dlp with its
YouTube challenge scripts (yt-dlp-ejs) and Deno, the JavaScript runtime that
solves them, as a wheel: nothing installed system-wide is used. FFmpeg is
Ceonggo's own; it turns whatever the site serves (Opus, AAC ...) into the
chosen format.

yt-dlp works in ``work_dir``, a folder only this job uses. The finished file is
moved into ``save_dir`` under a name no file there has, so a failed or stopped
run leaves no ``.part`` files beside the user's own, and nothing they had is
replaced. The window removes ``work_dir`` again if this process is killed.

YouTube's player clients are left to yt-dlp, which moves them as YouTube
changes. Forcing ``android`` and ``web`` (October 2026) found no audio-only
stream at all - YouTube's SABR experiment hides them from the android client -
and fell back to the 360p video's AAC track, where the defaults got Opus.

Job fields::

    url, save_dir, work_dir, output_format ("flac" or "mp3"), filename_template,
    ffmpeg (ffmpeg.exe; ffprobe is beside it), cache_dir (yt-dlp's own cache)

or, for Update downloader, which installs the newest yt-dlp from PyPI::

    update: true, uv (uv.exe), uv_cache
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _protocol as proto  # noqa: E402

#: Must equal EXPECTED_WORKER_VERSION in app/jobs/worker_proc.py.
WORKER_VERSION = 2

FORMATS = ("flac", "mp3")
DEFAULT_TEMPLATE = "%(title)s.%(ext)s"
#: yt-dlp calls its progress hook for every block it receives; the window needs a few a second.
PROGRESS_EVERY = 0.25
#: Update downloader moves these two: yt-dlp pins the challenge scripts to its own release.
UPDATE_PACKAGES = ("yt-dlp", "yt-dlp-ejs")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class Unsuitable(Exception):
    """A link this tool will not download; ``kind`` is what the window explains."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


def classify(message: str) -> str:
    """A yt-dlp failure -> a kind the window can explain. Order matters: an HTTP 403
    while fetching the page is the site refusing, not the network being down."""
    low = message.lower()
    if "unsupported url" in low or "is not a valid url" in low:
        return "bad_link"
    if any(s in low for s in ("private video", "video unavailable", "not available", "has been removed",
                              "confirm your age", "members-only", "http error 404", "http error 410")):
        return "unavailable"
    if any(s in low for s in ("sign in to confirm", "http error 403", "forbidden", "unable to extract",
                              "nsig", "signature", "challenge", "requested format is not available",
                              "no video formats found")):
        return "site_changed"
    if any(s in low for s in ("unable to download", "getaddrinfo", "timed out", "urlopen error",
                              "connection", "network is unreachable", "name resolution")):
        return "network"
    return proto.classify_error(message)


def unique_path(path: Path) -> Path:
    """``path``, else ``name (2).ext``, ``name (3).ext`` ... - never a file already there."""
    candidate, n = path, 1
    while candidate.exists():
        n += 1
        candidate = path.with_name(f"{path.stem} ({n}){path.suffix}")
    return candidate


def place(source: Path, target: Path) -> None:
    """Move a finished file to ``target``. Across drives it is copied to a ``.part``
    renamed only when complete, so ``target`` never exists half-written."""
    try:
        os.replace(source, target)
        return
    except OSError:
        pass
    part = target.with_name(target.name + ".part")
    try:
        shutil.copyfile(source, part)
        os.replace(part, target)
    finally:
        part.unlink(missing_ok=True)


def deno_path() -> str | None:
    """The Deno that came as a wheel with yt-dlp, never one on the system PATH."""
    try:
        from deno import find_deno_bin
        return find_deno_bin()
    except (ImportError, FileNotFoundError):
        return None


def run(job: dict) -> int:
    started = time.perf_counter()

    def stage(sid: str, label: str, completed, total, unit=None, status=None, **extra) -> None:
        proto.emit("stage", id=sid, label=label, completed=completed, total=total, unit=unit,
                   elapsed=round(time.perf_counter() - started, 2), status=status, **extra)

    if job.get("update"):
        return update(job, stage, started)
    # work_dir is deleted at the end, whatever happened: it must not be, or hold, the user's folder.
    save_dir, work_dir = Path(job["save_dir"]).resolve(), Path(job["work_dir"]).resolve()
    if save_dir.is_relative_to(work_dir) or work_dir.parent == work_dir:
        raise ValueError(f"work_dir must be a folder of its own, not {work_dir}")
    try:
        return download(job, stage, started)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def download(job: dict, stage, started: float) -> int:
    output_format = job.get("output_format") or "flac"
    if output_format not in FORMATS:
        raise ValueError(f"Unknown output format: {output_format}")
    ffmpeg = Path(job["ffmpeg"])
    if not ffmpeg.is_file():
        raise FileNotFoundError(f"ffmpeg not found: {ffmpeg}")
    save_dir, work_dir = Path(job["save_dir"]), Path(job["work_dir"])
    save_dir.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)

    stage("connect", "Connecting", 0, None)
    import yt_dlp
    from yt_dlp.utils import DownloadCancelled, DownloadError

    proto.emit("hello", worker="download", version=yt_dlp.version.__version__)
    last = 0.0

    def on_progress(d: dict) -> None:
        nonlocal last
        if proto.cancelled():
            raise DownloadCancelled("Cancelled")
        done = int(d.get("downloaded_bytes") or 0)
        total = d.get("total_bytes") or d.get("total_bytes_estimate")
        if d.get("status") == "finished":
            stage("download", "Downloading the audio", done, done, unit="bytes", status="completed")
            return
        now = time.monotonic()
        if d.get("status") != "downloading" or now - last < PROGRESS_EVERY:
            return
        last = now
        speed, eta = d.get("speed"), d.get("eta")
        stage("download", "Downloading the audio", done, int(total) if total else None, unit="bytes",
              speed=round(speed) if speed else None, eta=round(eta) if eta is not None else None)

    converting: set[str] = set()

    def on_postprocess(d: dict) -> None:
        # yt-dlp reports each step to the hook twice; the window needs each once.
        status = d.get("status")
        if d.get("postprocessor") != "ExtractAudio" or status in converting:
            return
        converting.add(status)
        if status == "started":
            if proto.cancelled():
                raise DownloadCancelled("Cancelled")
            stage("convert", "Converting the audio", 0, None)
        elif status == "finished":
            stage("convert", "Converting the audio", 1, 1, status="completed")

    def refuse_live(info: dict, *, incomplete: bool = False) -> None:
        # Asked before any format is chosen; a live stream would download until stopped.
        if info.get("live_status") in ("is_live", "is_upcoming") or info.get("is_live"):
            raise Unsuitable("live", "This link is a live stream")
        return None

    deno = deno_path()
    options = {
        "format": "bestaudio/best",
        "paths": {"home": str(work_dir)},
        "outtmpl": job.get("filename_template") or DEFAULT_TEMPLATE,
        "trim_file_name": 120,                 # long titles must not push the path past Windows' limit
        "ffmpeg_location": str(ffmpeg.parent),
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": output_format,
                            "preferredquality": "0"}],
        "progress_hooks": [on_progress],
        "postprocessor_hooks": [on_postprocess],
        "match_filter": refuse_live,
        "noplaylist": True,
        # work_dir is new, so the only .part to resume is a refused attempt's - and its fresh
        # links may be another stream with the same extension. (Retries inside one attempt still resume.)
        "continuedl": False,
        "cachedir": job.get("cache_dir") or False,
        # Only Ceonggo's own Deno; without it yt-dlp falls back to clients that need none.
        "js_runtimes": {"deno": {"path": deno}} if deno else {},
        "logtostderr": True,
        "noprogress": True,
    }
    with yt_dlp.YoutubeDL(options) as ydl:
        for attempt in (1, 2):
            info = ydl.extract_info(job["url"], download=False, process=False)
            if info.get("_type") in ("playlist", "multi_video"):
                raise Unsuitable("playlist", "This link is a playlist")
            if attempt == 1:
                stage("connect", "Connecting", 1, 1, status="completed")
            if proto.cancelled():
                raise InterruptedError("Cancelled")
            try:
                info = ydl.process_ie_result(info, download=True)
                break
            except DownloadError as error:
                # YouTube now and then refuses a media link it has only just handed out
                # (once in five downloads while this was written); fresh links worked.
                if attempt == 2 or "http error 403" not in str(error).lower() or proto.cancelled():
                    raise
                print("HTTP 403 on the media - asking for new links and trying once more", file=sys.stderr)
    if info.get("_type") in ("playlist", "multi_video"):   # a short link that turned out to be one
        raise Unsuitable("playlist", "This link is a playlist")
    downloads = info.get("requested_downloads") or []
    produced = Path(downloads[-1].get("filepath") or "") if downloads else None
    if produced is None or not produced.is_file():
        raise RuntimeError("yt-dlp finished without writing an audio file")
    if proto.cancelled():
        raise InterruptedError("Cancelled")
    target = unique_path(save_dir / produced.name)
    place(produced, target)
    proto.emit("done", file=str(target), title=info.get("title") or target.stem,
               seconds=round(time.perf_counter() - started, 1))
    return 0


def installed_version() -> str:
    """yt-dlp's version as installed now - asked in a fresh process, since uv has just changed it."""
    result = subprocess.run([sys.executable, "-c", "from importlib.metadata import version; print(version('yt-dlp'))"],
                            capture_output=True, text=True, creationflags=NO_WINDOW)
    return result.stdout.strip() if result.returncode == 0 else ""


def update(job: dict, stage, started: float) -> int:
    """Install the newest yt-dlp from PyPI into this runtime, for when a site changes under it.

    Only yt-dlp and its challenge scripts move; anything else only if the new
    release needs it. Stop counts until uv starts: a half-installed yt-dlp would
    be worse than an old one, and uv takes a few seconds.
    """
    stage("update", "Updating the downloader", 0, None)
    before = installed_version()
    if proto.cancelled():
        raise InterruptedError("Cancelled")
    env = {key: value for key, value in os.environ.items() if not key.startswith(("UV_", "VIRTUAL_ENV"))}
    env.update(UV_CACHE_DIR=job["uv_cache"], UV_NO_CONFIG="1", UV_PYTHON_DOWNLOADS="never")
    upgrade = [arg for name in UPDATE_PACKAGES for arg in ("--upgrade-package", name)]
    command = [job["uv"], "pip", "install", "--python", sys.executable, "--link-mode", "copy", *upgrade,
               "yt-dlp[default,deno]"]
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace",
                            env=env, creationflags=NO_WINDOW)
    sys.stderr.write(result.stdout + result.stderr)
    if result.returncode:
        raise RuntimeError(f"uv could not update yt-dlp: {result.stderr.strip()[-600:]}")
    stage("update", "Updating the downloader", 1, 1, status="completed")
    proto.emit("done", before=before, after=installed_version(), seconds=round(time.perf_counter() - started, 1))
    return 0


def main() -> int:
    proto.claim_stdout()
    try:
        return run(proto.read_job())
    except BaseException as exc:  # noqa: BLE001 - report everything to the window
        message = f"{type(exc).__name__}: {exc}"
        if isinstance(exc, (InterruptedError, KeyboardInterrupt)) or proto.cancelled():
            kind = "cancelled"
        elif isinstance(exc, Unsuitable):
            kind = exc.kind
        elif isinstance(exc, ModuleNotFoundError):
            kind = "missing_files"               # yt-dlp is not in this runtime yet: Repair adds it
        else:
            kind = classify(message)
        traceback.print_exc()
        proto.emit("error", kind=kind, message=message)
        return 2 if kind == "cancelled" else 1


if __name__ == "__main__":
    raise SystemExit(main())
