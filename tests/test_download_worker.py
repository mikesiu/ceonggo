"""workers/download_worker.py against a stand-in for yt-dlp: no network, no runtime.

The fake writes its download the way yt-dlp does - a growing ``.part`` file in
the folder it was given, the progress hook after every block, then the audio
conversion reported (twice, as yt-dlp does) to the postprocessor hook.
"""
import json
import subprocess
import sys
import types
from pathlib import Path

import pytest

import _protocol as proto
import download_worker as worker
from app.jobs import requests as rq

CHUNK = 1000


class FakeYoutubeDL:
    info: dict = {}
    on_chunk = staticmethod(lambda n: None)
    created: list = []
    refusals = 0                  # media requests YouTube answers with HTTP 403 before one works

    def __init__(self, params):
        self.params = params
        self.downloaded = False
        self.extracted = 0
        FakeYoutubeDL.created.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download=True, process=True):
        assert (download, process) == (False, False)        # the worker looks before it downloads
        self.extracted += 1
        return dict(self.info, webpage_url=url)

    def process_ie_result(self, info, download=True):
        assert download
        self.params["match_filter"](info, incomplete=False)
        if FakeYoutubeDL.refusals:
            FakeYoutubeDL.refusals -= 1
            raise DownloadError("ERROR: unable to download video data: HTTP Error 403: Forbidden")
        self.downloaded = True
        home = Path(self.params["paths"]["home"])
        name = self.params["outtmpl"].replace("%(title)s", info["title"]).replace("%(ext)s", "webm")
        part, total = home / f"{name}.part", 4 * CHUNK
        for n in range(1, 5):
            part.write_bytes(b"x" * CHUNK * n)
            for hook in self.params["progress_hooks"]:
                hook({"status": "downloading", "downloaded_bytes": CHUNK * n, "total_bytes": total,
                      "speed": 2048.0, "eta": 4 - n})
            self.on_chunk(n)
        source = part.rename(home / name)
        for hook in self.params["progress_hooks"]:
            hook({"status": "finished", "downloaded_bytes": total, "total_bytes": total})
        for status in ("started", "started", "finished", "finished"):
            for hook in self.params["postprocessor_hooks"]:
                hook({"status": status, "postprocessor": "ExtractAudio"})
        codec = self.params["postprocessors"][0]["preferredcodec"]
        audio = source.with_suffix(f".{codec}")
        audio.write_bytes(b"fLaC")
        source.unlink()
        return dict(info, requested_downloads=[{"filepath": str(audio)}])


class DownloadCancelled(Exception):
    pass


class DownloadError(Exception):
    pass


@pytest.fixture()
def fake_ytdlp(monkeypatch):
    module = types.ModuleType("yt_dlp")
    module.YoutubeDL = FakeYoutubeDL
    module.version = types.SimpleNamespace(__version__="2026.08.19")
    utils = types.ModuleType("yt_dlp.utils")
    utils.DownloadCancelled = DownloadCancelled
    utils.DownloadError = DownloadError
    module.utils = utils
    monkeypatch.setitem(sys.modules, "yt_dlp", module)
    monkeypatch.setitem(sys.modules, "yt_dlp.utils", utils)
    monkeypatch.setattr(FakeYoutubeDL, "info", {"title": "Me at the zoo", "id": "jNQXAC9IVRw"})
    monkeypatch.setattr(FakeYoutubeDL, "on_chunk", staticmethod(lambda n: None))
    monkeypatch.setattr(FakeYoutubeDL, "created", [])
    monkeypatch.setattr(FakeYoutubeDL, "refusals", 0)
    monkeypatch.setattr(worker, "deno_path", lambda: r"C:\Ceonggo\runtime\sheetsage\Scripts\deno.exe")
    monkeypatch.setattr(worker, "PROGRESS_EVERY", 0)
    return module


@pytest.fixture()
def events(monkeypatch):
    """Run the worker as the window would: the job file on the command line, events collected."""
    seen = []
    monkeypatch.setattr(proto, "emit", lambda kind, /, **fields: seen.append({"type": kind, **fields}))
    monkeypatch.setattr(proto, "claim_stdout", lambda: None)
    for name, value in (("_cancel_file", None), ("_cancel_seen", False), ("_cancel_checked", 0.0)):
        monkeypatch.setattr(proto, name, value)
    return seen


def start(tmp_path, monkeypatch, **fields) -> tuple[int, dict]:
    ffmpeg = tmp_path / "runtime" / "ffmpeg" / "bin" / "ffmpeg.exe"
    ffmpeg.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg.write_bytes(b"")
    job = {"url": "https://www.youtube.com/watch?v=jNQXAC9IVRw", "save_dir": str(tmp_path / "Downloads"),
           "work_dir": str(tmp_path / "cache" / "downloads" / "job-1"), "output_format": "flac",
           "filename_template": "%(title)s.%(ext)s", "ffmpeg": str(ffmpeg), "cache_dir": str(tmp_path / "cache" / "yt-dlp"),
           **fields}
    path = tmp_path / "job.json"
    path.write_text(json.dumps(job), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["download_worker.py", str(path)])
    return worker.main(), job


def stages(seen, sid):
    return [e for e in seen if e["type"] == "stage" and e["id"] == sid]


def test_progress_and_one_finished_file(tmp_path, monkeypatch, fake_ytdlp, events):
    code, job = start(tmp_path, monkeypatch)
    assert code == 0
    saved = tmp_path / "Downloads" / "Me at the zoo.flac"
    assert events[-1] == {"type": "done", "file": str(saved), "title": "Me at the zoo", "seconds": events[-1]["seconds"]}
    assert saved.read_bytes() == b"fLaC"
    assert [p.name for p in saved.parent.iterdir()] == [saved.name]          # no .part, no .webm
    assert not Path(job["work_dir"]).exists()

    downloading = stages(events, "download")
    assert [(e["completed"], e["total"]) for e in downloading] == [(1000, 4000), (2000, 4000), (3000, 4000),
                                                                    (4000, 4000), (4000, 4000)]
    assert {e["unit"] for e in downloading} == {"bytes"} and downloading[0]["speed"] == 2048
    assert downloading[0]["eta"] == 3 and downloading[-1]["status"] == "completed"
    assert [(e["completed"], e["status"]) for e in stages(events, "convert")] == [(0, None), (1, "completed")]
    assert stages(events, "connect")[-1]["status"] == "completed"
    assert {e["id"] for e in events if e["type"] == "stage"} <= {sid for sid, _ in rq.DOWNLOAD_STAGES}

    params = FakeYoutubeDL.created[0].params
    assert params["format"] == "bestaudio/best" and params["noplaylist"] is True
    assert params["ffmpeg_location"] == str(Path(job["ffmpeg"]).parent)        # ffprobe is found beside it
    assert params["postprocessors"] == [{"key": "FFmpegExtractAudio", "preferredcodec": "flac",
                                         "preferredquality": "0"}]
    assert params["cachedir"] == job["cache_dir"]
    assert params["continuedl"] is False                   # a retry never splices onto a refused .part
    assert params["js_runtimes"] == {"deno": {"path": r"C:\Ceonggo\runtime\sheetsage\Scripts\deno.exe"}}
    assert "extractor_args" not in params                  # YouTube's clients are yt-dlp's choice


def test_mp3_is_asked_of_ffmpeg(tmp_path, monkeypatch, fake_ytdlp, events):
    assert start(tmp_path, monkeypatch, output_format="mp3")[0] == 0
    assert FakeYoutubeDL.created[0].params["postprocessors"][0]["preferredcodec"] == "mp3"
    assert (tmp_path / "Downloads" / "Me at the zoo.mp3").is_file()


def test_stop_mid_download_leaves_no_partial_files(tmp_path, monkeypatch, fake_ytdlp, events):
    def press_stop(n):
        if n == 2:
            proto.cancel_path(tmp_path / "job.json").write_text("cancel")
            monkeypatch.setattr(proto, "_cancel_checked", 0.0)      # the next check reads the file

    monkeypatch.setattr(FakeYoutubeDL, "on_chunk", staticmethod(press_stop))
    code, job = start(tmp_path, monkeypatch)
    assert code == 2
    assert events[-1]["type"] == "error" and events[-1]["kind"] == "cancelled"
    assert [e["completed"] for e in stages(events, "download")] == [1000, 2000]    # stopped at the next block
    assert not [e for e in events if e["type"] == "done"]
    assert not Path(job["work_dir"]).exists()                                   # the .part went with it
    assert list((tmp_path / "Downloads").iterdir()) == []


def test_a_file_already_there_is_never_replaced(tmp_path, monkeypatch, fake_ytdlp, events):
    mine = tmp_path / "Downloads" / "Me at the zoo.flac"
    mine.parent.mkdir()
    mine.write_bytes(b"mine")
    assert start(tmp_path, monkeypatch)[0] == 0
    assert mine.read_bytes() == b"mine"
    assert events[-1]["file"] == str(mine.with_name("Me at the zoo (2).flac"))
    assert worker.unique_path(mine) == mine.with_name("Me at the zoo (3).flac")


def test_a_refused_media_link_is_asked_for_again_once(tmp_path, monkeypatch, fake_ytdlp, events):
    monkeypatch.setattr(FakeYoutubeDL, "refusals", 1)
    assert start(tmp_path, monkeypatch)[0] == 0
    assert FakeYoutubeDL.created[0].extracted == 2                     # fresh links, not the refused ones
    assert len(stages(events, "connect")) == 2                         # the card does not step back
    assert (tmp_path / "Downloads" / "Me at the zoo.flac").is_file()

    events.clear()
    monkeypatch.setattr(FakeYoutubeDL, "refusals", 2)                   # refused again: say so
    code, job = start(tmp_path, monkeypatch)
    assert code == 1 and events[-1]["kind"] == "site_changed"
    assert not Path(job["work_dir"]).exists()


@pytest.mark.parametrize("info, kind", [
    ({"_type": "playlist", "title": "Mix", "entries": []}, "playlist"),
    ({"title": "Radio", "live_status": "is_live"}, "live"),
])
def test_playlists_and_live_streams_are_refused(tmp_path, monkeypatch, fake_ytdlp, events, info, kind):
    monkeypatch.setattr(FakeYoutubeDL, "info", info)
    code, job = start(tmp_path, monkeypatch)
    assert code == 1 and events[-1]["kind"] == kind
    assert not FakeYoutubeDL.created[0].downloaded
    assert not Path(job["work_dir"]).exists()
    assert rq.friendly_error(events[-1]) != rq.friendly_error({"kind": "unknown"})


def test_work_dir_can_never_be_the_users_folder(tmp_path, monkeypatch, fake_ytdlp, events):
    """work_dir is deleted when the job ends, so a job that names the save folder,
    or a folder holding it, must fail before anything is touched."""
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    (downloads / "keep.flac").write_bytes(b"mine")
    for work_dir in (downloads, tmp_path):
        events.clear()
        assert start(tmp_path, monkeypatch, work_dir=str(work_dir))[0] == 1
        assert events[-1]["type"] == "error" and "work_dir" in events[-1]["message"]
        assert (downloads / "keep.flac").read_bytes() == b"mine"
    assert not FakeYoutubeDL.created


def test_a_runtime_without_ytdlp_asks_for_repair(tmp_path, monkeypatch, events):
    monkeypatch.setitem(sys.modules, "yt_dlp", None)              # import raises ModuleNotFoundError
    assert start(tmp_path, monkeypatch)[0] == 1
    assert events[-1]["kind"] == "missing_files"


@pytest.mark.parametrize("message, kind", [
    ("DownloadError: ERROR: Unsupported URL: https://example.com/", "bad_link"),
    ("DownloadError: ERROR: [youtube] abc: Private video. Sign in if you've been granted access", "unavailable"),
    ("DownloadError: ERROR: [youtube] abc: Sign in to confirm your age. This video may be inappropriate",
     "unavailable"),
    ("DownloadError: ERROR: [generic] Unable to download webpage: HTTP Error 404: Not Found", "unavailable"),
    ("DownloadError: ERROR: [youtube] abc: Sign in to confirm you’re not a bot", "site_changed"),
    ("DownloadError: ERROR: unable to download video data: HTTP Error 403: Forbidden", "site_changed"),
    ("DownloadError: ERROR: [youtube] abc: No video formats found!; please report this issue", "site_changed"),
    ("DownloadError: ERROR: [youtube] abc: Unable to download webpage: <urlopen error [Errno 11001] "
     "getaddrinfo failed>", "network"),
])
def test_failures_are_explained(message, kind):
    assert worker.classify(message) == kind
    assert rq.friendly_error({"kind": kind, "message": message}) != rq.friendly_error({"kind": "unknown"})


def test_update_uses_ceonggos_own_uv(tmp_path, monkeypatch, events):
    calls = []
    versions = iter(["2026.8.19\n", "2026.9.30\n"])

    def run(command, **kwargs):
        calls.append((command, kwargs))
        out = next(versions) if command[0] == sys.executable else "Installed 1 package"
        return subprocess.CompletedProcess(command, 0, stdout=out, stderr="")

    monkeypatch.setattr(worker.subprocess, "run", run)
    code, job = start(tmp_path, monkeypatch, update=True, uv=str(tmp_path / "runtime" / "uv" / "uv.exe"),
                      uv_cache=str(tmp_path / "cache" / "uv"))
    assert code == 0
    assert events[-1]["type"] == "done" and (events[-1]["before"], events[-1]["after"]) == ("2026.8.19", "2026.9.30")
    command, kwargs = calls[1]
    assert command[:5] == [job["uv"], "pip", "install", "--python", sys.executable]   # this runtime, no other
    assert command[-5:] == ["--upgrade-package", "yt-dlp", "--upgrade-package", "yt-dlp-ejs", "yt-dlp[default,deno]"]
    assert kwargs["env"]["UV_CACHE_DIR"] == job["uv_cache"] and kwargs["env"]["UV_NO_CONFIG"] == "1"
    assert [e["status"] for e in stages(events, "update")] == [None, "completed"]


def test_jobs_keep_the_users_folder_out_of_output_dir(tmp_path, monkeypatch):
    """The Engine may empty a job's output_dir before a retry; the user's Downloads must never be one."""
    monkeypatch.setenv("CEONGGO_ROOT", str(tmp_path))
    job = rq.download_job(url="https://youtu.be/x", save_dir=tmp_path / "Downloads",
                          work_dir=tmp_path / "cache" / "downloads" / "1", output_format="mp3")
    assert "output_dir" not in job and job["save_dir"] == str(tmp_path / "Downloads")
    assert job["output_format"] == "mp3" and job["ffmpeg"].endswith("ffmpeg.exe")
    assert job["cache_dir"].startswith(str(tmp_path))                             # portable, not %APPDATA%
    update = rq.downloader_update_job()
    assert update["update"] is True and update["uv"].endswith("uv.exe") and update["uv_cache"].startswith(str(tmp_path))
    assert {sid for sid, _ in rq.DOWNLOAD_STAGES} == set(rq.DOWNLOAD_WEIGHT)


def test_transfer_sizes_read_like_a_download():
    from app.ui.progress_card import size_text

    assert [size_text(n) for n in (812, 348_365, 4_299_161, 3 * 2**30)] == ["812 B", "340.2 KB", "4.1 MB", "3.00 GB"]
