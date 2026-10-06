"""Turn what the user chose into worker jobs, and failures into plain words."""
from __future__ import annotations

import random

from app import gpu, paths
from app.config import Config
from app.i18n import N, t


#: Older settings files stored a GB figure; map them onto the modes.
LEGACY_PERFORMANCE = {"24": "fast", "16": "balanced", "12": "low"}


def memory_mode(cfg: Config) -> str:
    choice = cfg.get("performance", "auto")
    choice = LEGACY_PERFORMANCE.get(choice, choice)
    if choice in gpu.MEMORY_MODES:
        return choice
    return gpu.auto_memory(gpu.detect())


def backend(cfg: Config) -> str:
    """Which decoding path the worker should take.

    "torch" drives YuE2's CUDA-graph decoder, the fastest thing on an NVIDIA card.
    On AMD it is a pessimisation: ROCm has no graph-friendly attention kernel, so
    GraphAR falls back to SDPA and captures it anyway. Measured on a Radeon AI PRO
    R9700, the same song took 293.6s that way against 151.2s in eager mode - the
    singing stage alone went from 96.4s to 28.0s. So an AMD card gets eager unless
    the user has deliberately picked something else in Settings.
    """
    choice = cfg.get("backend", "torch")
    if choice != "torch":
        return choice
    card = gpu.detect()
    return "torch-eager" if card is not None and card.vendor == "amd" else choice


def new_seed() -> int:
    return random.randint(1, 2**31 - 1)


def take_seeds(count: int, first: int | None = None) -> list[int]:
    """One seed per take, all different.

    With ``first`` (Keep this seed), takes use first, first+1, ... so the first take
    reproduces the song; otherwise every take gets a fresh random seed.
    """
    if first is not None:
        return [(int(first) - 1 + i) % (2**31 - 1) + 1 for i in range(count)]
    seeds: list[int] = []
    while len(seeds) < count:
        seed = new_seed()
        if seed not in seeds:
            seeds.append(seed)
    return seeds


def song_job(cfg: Config, *, style: str, lyrics: str, cot: str, seed: int, output_dir,
             abc: str | None = None, stage: str = "audio", cfg_scale: float | None = None) -> dict:
    decoder = "YuE2-Vae-legacy" if cfg.get("decoder") == "legacy" else "YuE2-Vae"
    return {
        "style": style, "lyrics": lyrics, "cot": cot, "seed": int(seed), "abc": abc,
        "cfg_scale": cfg_scale, "stage": stage, "output_dir": str(output_dir),
        "model_dir": str(paths.models_dir() / "YuE2-3B"),
        "vae_dir": str(paths.models_dir() / decoder),
        "memory": memory_mode(cfg),
        "backend": backend(cfg), "verify_hashes": False,
    }


def transcribe_job(*, audio: str, output_dir, vocal_only: bool = False) -> dict:
    return {
        "audio": str(audio), "output_dir": str(output_dir), "melody_only": False,
        "vocal_only": vocal_only,
        "model_dir": str(paths.models_dir() / "SheetSage2"),
        "mert_dir": str(paths.models_dir() / "MERT-v2-FullSong"),
    }


def remix_job(*, song_audio, vocal, output_dir, vocal_offset: float = 0.0) -> dict:
    """Replace vocal: the song's own band under a vocal sung elsewhere (OpenUtau).

    ``vocal_offset`` is the song's clock shift: YuE2 sang off the score's clock,
    the new vocal is on it (see app/score/timing.py).
    """
    return {
        "song_audio": str(song_audio), "vocal": str(vocal), "output_dir": str(output_dir),
        "vocal_offset": float(vocal_offset), "vocal_gain_db": 0.0,
        "model": str(paths.separator_model()), "ffmpeg": str(paths.ffmpeg_bin() / "ffmpeg.exe"),
    }


def extract_job(*, audio, vocals, instrumental) -> dict:
    """Tools → Extract vocals: any recording split into two files, its vocal and its instrumental."""
    return {
        "audio": str(audio), "vocals": str(vocals), "instrumental": str(instrumental),
        "model": str(paths.separator_model()), "ffmpeg": str(paths.ffmpeg_bin() / "ffmpeg.exe"),
    }


def download_job(*, url: str, save_dir, work_dir, output_format: str = "flac") -> dict:
    """Tools → Download audio: one link's audio as FLAC or MP3, into the user's own folder.

    ``save_dir`` is not called output_dir on purpose: the Engine may empty a job's
    output_dir before a retry, and this one is the user's Downloads. ``work_dir``
    is a fresh folder under the cache that the worker deletes when it ends.
    """
    return {
        "url": url, "save_dir": str(save_dir), "work_dir": str(work_dir), "output_format": output_format,
        "filename_template": "%(title)s.%(ext)s", "ffmpeg": str(paths.ffmpeg_bin() / "ffmpeg.exe"),
        "cache_dir": str(paths.cache_dir() / "yt-dlp"),
    }


def downloader_update_job() -> dict:
    """Download audio's Update downloader: the newest yt-dlp from PyPI, installed with Ceonggo's uv."""
    return {"update": True, "uv": str(paths.uv_exe()), "uv_cache": str(paths.cache_dir() / "uv")}


#: Worker stage ids -> what a musician is told. Order is the order they happen.
STAGES = [
    ("load", N("Warming up the band")),
    ("plan", N("Writing the melody and chords")),
    ("score", N("Reading your score")),
    ("sing", N("Performing the song")),
    ("synth", N("Mixing the sound")),
    ("decode", N("Rendering the audio")),
]
STAGE_ALIASES = {"resolve": "load", "verify": "load", "decoder": "decode"}

#: Rough share of the whole run each stage takes (measured on an RTX A4000).
STAGE_WEIGHT = {"load": 0.12, "plan": 0.10, "score": 0.02, "sing": 0.30, "synth": 0.38, "decode": 0.08}

TRANSCRIBE_STAGES = [
    ("load", N("Loading the listener")),
    ("transcribe_audio", N("Reading the recording")),
    ("transcribe_encoding", N("Listening")),
    ("transcribe_decoding", N("Writing down the melody")),
    ("transcribe_notation", N("Writing the score")),
]


REMIX_STAGES = [
    ("load", N("Loading the separator")),
    ("separate", N("Extracting instrumental")),
    ("mix", N("Mixing new vocals")),
]
#: Measured on a Radeon AI PRO R9700 for a 3:12 song: 0.7 s, 7 s, 9 s.
REMIX_WEIGHT = {"load": 0.05, "separate": 0.45, "mix": 0.5}

EXTRACT_STAGES = [
    ("load", N("Loading the separator")),
    ("read", N("Reading the recording")),
    ("separate", N("Separating the vocals")),
    ("save", N("Saving the vocals and instrumental")),
]
#: Separating is nearly all of it (about 7 s of a 3:12 song on a Radeon AI PRO R9700).
EXTRACT_WEIGHT = {"load": 0.05, "read": 0.05, "separate": 0.8, "save": 0.1}

#: "update" is Update downloader's only stage; a download skips it, and it skips the rest.
DOWNLOAD_STAGES = [
    ("connect", N("Connecting")),
    ("download", N("Downloading the audio")),
    ("convert", N("Converting the audio")),
    ("update", N("Updating the downloader")),
]
DOWNLOAD_WEIGHT = {"connect": 0.1, "download": 0.75, "convert": 0.15, "update": 1.0}


def friendly_error(event: dict) -> str:
    kind = event.get("kind", "unknown")
    message = event.get("message", "")
    if kind == "cancelled":
        return t("Stopped.")
    if kind == "out_of_memory":
        return t("The graphics card ran out of memory, even in the lowest-memory mode. Close other "
                 "programs that use the GPU (games, browsers, video apps) or try shorter lyrics.")
    if kind == "no_gpu":
        return t("No supported graphics card is available to the engine. Check the driver is installed.")
    if kind == "driver":
        return t("The graphics driver is too old for the engine. Update it from nvidia.com.")
    if kind == "missing_files":
        return t("Some engine files are missing. Open Settings → Repair to run Ceonggo Setup.")
    if kind == "no_score":
        return t("The melody could not be written down from this recording ({reason}). "
                 "Try a clearer recording, or a different part of the song.", reason=message)
    if kind == "mixed_versions":
        return t("This Ceonggo folder mixes files from two versions, so the engine could not start. "
                 "Replace Ceonggo.exe, Ceonggo Setup.exe and the whole _internal folder from the new release "
                 "(keep runtime, models and songs).")
    if kind == "crashed":
        return t("The engine stopped unexpectedly. The details are in the log folder.")
    # Download audio (workers/download_worker.py).
    if kind == "bad_link":
        return t("No audio was found at this link. Check it opens a single video or track.")
    if kind == "unavailable":
        return t("This video is private, removed, age-restricted or not available here.")
    if kind == "site_changed":
        return t("The site refused the download. It may have changed since this downloader was made, "
                 "or be limiting downloads for now. Press Update downloader and try again, or try later.")
    if kind == "network":
        return t("The site could not be reached. Check the internet connection and try again.")
    if kind == "playlist":
        return t("This link is a playlist. Paste the link of one video from it.")
    if kind == "live":
        return t("This link is a live stream. Download it once the stream has ended.")
    return t("Something went wrong: {message}", message=message)
