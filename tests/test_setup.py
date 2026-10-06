import http.server
import io
import json
import tarfile
import threading
import zipfile
from functools import partial
from pathlib import Path

import pytest

import build_manifest
from setup import download as dl
from setup.catalog import Catalog
from setup.steps import Installer, unpack

ROOT = Path(__file__).resolve().parent.parent


def test_catalog_components_and_order():
    catalog = Catalog(ROOT / "setup" / "catalog.json")
    ids = {c.id for c in catalog.components}
    assert {"song", "cover"} <= ids
    song_only = {i.id for i in catalog.items_for([])}          # required component always included
    assert "model:YuE2-3B" in song_only and "env:sheetsage" not in song_only
    installer = Installer(catalog, Path("Z:/does-not-exist"), ["song", "cover"])
    kinds = [i.kind for i in installer.plan()]
    assert kinds == sorted(kinds, key=["model", "archive", "wheels"].index)
    archives = [i.id for i in installer.plan() if i.kind == "archive"]
    assert archives[0] == "tool:uv"


def test_catalogue_offers_one_environment_per_card():
    catalog = Catalog(ROOT / "setup" / "catalog.json")
    for vendor, wanted in (("nvidia", {"env:yue2", "env:sheetsage"}),
                           ("amd", {"env:yue2-rocm", "env:sheetsage-rocm"})):
        envs = {i.id for i in catalog.items_for(["song", "cover"], vendor) if i.kind == "wheels"}
        assert envs == wanted, vendor
    # everything that is not a library environment is shared, so it carries no
    # variant and must be offered to both
    shared = {i.id for i in catalog.items if not i.variant}
    assert {"model:YuE2-3B", "python:yue2", "tool:ffmpeg"} <= shared
    assert not any(i.variant for i in catalog.items if i.kind != "wheels")


def test_repair_on_an_amd_card_leaves_the_cuda_environment_alone(tmp_path):
    """Setup used to drop CUDA wheels over a working ROCm runtime, which is how
    an AMD install broke itself the moment anyone pressed Repair."""
    catalog = Catalog(ROOT / "setup" / "catalog.json")
    amd = Installer(catalog, tmp_path, ["song", "cover"], vendor="amd")
    ids = {i.id for i in amd.items}
    assert "env:yue2" not in ids and "env:sheetsage" not in ids
    assert {"env:yue2-rocm", "env:sheetsage-rocm"} <= ids
    assert {i.id for i in amd.plan()} == ids          # nothing installed yet, so all of it
    nvidia = Installer(catalog, tmp_path, ["song", "cover"], vendor="nvidia")
    assert not any(i.id.endswith("-rocm") for i in nvidia.items)


def test_a_component_is_complete_on_either_card(tmp_path):
    """Completeness is judged per card. Counting both variants would leave every
    component for ever incomplete, and the app would say it was not installed."""
    catalog = Catalog(ROOT / "setup" / "catalog.json")
    for vendor in ("nvidia", "amd"):
        root = tmp_path / vendor
        root.mkdir()
        installer = Installer(catalog, root, ["song", "cover"], vendor=vendor)
        installer.write_state()
        state = json.loads((root / "setup_state.json").read_text())
        assert "song" in state["components"] and "cover" in state["components"], vendor


def test_every_download_is_pinned():
    catalog = Catalog(ROOT / "setup" / "catalog.json")
    for item in catalog.items:
        for f in item.files:
            assert f["size"] > 0, f
            if item.kind != "model" or f["size"] > 10_000_000:
                assert len(f["sha256"]) == 64, f
        if item.kind == "model":
            for f in item.files:
                if "huggingface.co" in f["url"]:
                    assert "/resolve/" + item.data["revision"] + "/" in f["url"]
                else:                  # a release file (the separator): no LFS oid, so hash it all
                    assert len(f["sha256"]) == 64, f


def test_pick_wheel_prefers_platform_wheels():
    wheels = [{"url": "https://x/soundfile-0.13.1-py2.py3-none-any.whl"},
              {"url": "https://x/soundfile-0.13.1-py2.py3-none-win_amd64.whl"},
              {"url": "https://x/soundfile-0.13.1-py2.py3-none-macosx_11_0_arm64.whl"}]
    assert build_manifest.pick_wheel(wheels, "cp312")["url"].endswith("win_amd64.whl")
    wheels = [{"url": "https://x/numpy-2.2.6-cp311-cp311-win_amd64.whl"},
              {"url": "https://x/numpy-2.2.6-cp312-cp312-win_amd64.whl"}]
    assert "cp312-cp312" in build_manifest.pick_wheel(wheels, "cp312")["url"]
    assert build_manifest.pick_wheel([{"url": "https://x/a-1-cp312-cp312-manylinux_x86_64.whl"}], "cp312") is None


def test_unpack_strips_top_folder(tmp_path):
    archive = tmp_path / "a.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("top/bin/ffmpeg.exe", b"x")
        z.writestr("other/skip.txt", b"y")
        z.writestr("top/../evil.txt", b"z")
    unpack(archive, tmp_path / "out", "top")
    assert (tmp_path / "out" / "bin" / "ffmpeg.exe").read_bytes() == b"x"
    assert not (tmp_path / "out" / "skip.txt").exists()
    assert not (tmp_path / "evil.txt").exists()

    tgz = tmp_path / "p.tar.gz"
    with tarfile.open(tgz, "w:gz") as tar:
        data = b"python"
        info = tarfile.TarInfo("python/python.exe")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    unpack(tgz, tmp_path / "py", "python")
    assert (tmp_path / "py" / "python.exe").read_bytes() == b"python"


def test_unpack_refuses_an_entry_that_escapes(tmp_path):
    """A drive-absolute name replaces the destination when joined, so "C:/x"
    lands outside the folder even though it holds no ".." and no leading "/"."""
    archive = tmp_path / "b.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("top/bin/good.txt", b"ok")
        z.writestr("top/C:/escaped.txt", b"no")
    out = tmp_path / "out"
    unpack(archive, out, "top")
    assert (out / "bin" / "good.txt").read_bytes() == b"ok"
    assert [p.name for p in sorted(out.rglob("*")) if p.is_file()] == ["good.txt"]
    assert not Path("C:/escaped.txt").exists()


class RangeHandler(http.server.SimpleHTTPRequestHandler):
    """Serves files with Range support, which http.server lacks."""

    def log_message(self, *args):
        pass

    def do_GET(self):
        path = Path(self.translate_path(self.path))
        data = path.read_bytes()
        start = 0
        if "Range" in self.headers:
            start = int(self.headers["Range"].split("=")[1].split("-")[0])
            self.send_response(206)
        else:
            self.send_response(200)
        body = data[start:]
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture()
def server(tmp_path):
    root = tmp_path / "srv"
    root.mkdir()
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), partial(RangeHandler, directory=str(root)))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield root, f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def test_download_resumes_and_verifies(server, tmp_path):
    import hashlib

    root, base = server
    payload = bytes(range(256)) * 20_000          # ~5 MB
    (root / "model.bin").write_bytes(payload)
    sha = hashlib.sha256(payload).hexdigest()
    target = tmp_path / "dl" / "model.bin"
    target.parent.mkdir()
    # A previous attempt left the first 1.5 MB behind.
    target.with_suffix(".bin.part").write_bytes(payload[:1_500_000])
    seen = []
    dl.download(f"{base}/model.bin", target, expected_bytes=len(payload), sha256=sha,
                on_progress=lambda p: seen.append(p.done))
    assert target.read_bytes() == payload
    assert seen and min(seen) >= 1_500_000              # continued, did not restart

    bad = tmp_path / "dl" / "bad.bin"
    with pytest.raises(dl.DownloadError):
        dl.download(f"{base}/model.bin", bad, expected_bytes=len(payload), sha256="0" * 64)
    assert not bad.exists()


def test_download_cancel_keeps_part(server, tmp_path):
    root, base = server
    (root / "big.bin").write_bytes(b"\0" * 8_000_000)
    target = tmp_path / "big.bin"
    with pytest.raises(dl.Cancelled):
        dl.download(f"{base}/big.bin", target, expected_bytes=8_000_000,
                    should_stop=lambda: target.with_suffix(".bin.part").exists()
                    and target.with_suffix(".bin.part").stat().st_size > 0)
    assert target.with_suffix(".bin.part").exists() and not target.exists()


def test_download_drops_a_part_that_cannot_grow(server, tmp_path):
    """A wrong-size .part used to be kept for ever, so every later run resumed
    past the server's end, got nothing back, and failed the same check again."""
    root, base = server
    (root / "short.bin").write_bytes(b"\xaa" * 800)        # the catalogue says 1000
    target = tmp_path / "short.bin"
    part = target.with_suffix(".bin.part")

    with pytest.raises(dl.DownloadError):
        dl.download(f"{base}/short.bin", target, expected_bytes=1000)
    assert part.stat().st_size == 800          # it grew this run, so it is kept

    with pytest.raises(dl.DownloadError):
        dl.download(f"{base}/short.bin", target, expected_bytes=1000)
    assert not part.exists()                   # it could not grow, so it is dropped
    assert not target.exists()


def test_a_dev_venv_cannot_leak_into_setup(tmp_path, monkeypatch):
    """uv installs into a standalone Python by path; an inherited VIRTUAL_ENV
    would send the wheels somewhere else entirely."""
    for name in ("VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME"):
        monkeypatch.setenv(name, r"D:\leak")
    env = Installer(Catalog(ROOT / "setup" / "catalog.json"), tmp_path, []).environment()
    assert not [k for k in env if k in ("VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME")]
    assert env["UV_CACHE_DIR"].startswith(str(tmp_path))


def test_install_state(tmp_path, monkeypatch):
    monkeypatch.setenv("CEONGGO_ROOT", str(tmp_path))
    from app.install_state import InstallState

    assert not InstallState.load().ready
    (tmp_path / "setup_state.json").write_text(json.dumps({"components": ["song"]}))
    state = InstallState.load()
    assert not state.ready and "song" in state.missing        # recorded but files gone
    for f in ("runtime/yue2/python.exe", "models/YuE2-3B/model.safetensors",
              "models/YuE2-Vae/model.safetensors"):
        (tmp_path / f).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / f).write_bytes(b"x")
    assert InstallState.load().ready
