"""What graphics card this machine has, asked of the driver.

Neither Ceonggo nor Ceonggo Setup imports torch, so the card is identified the
way the driver itself reports it - which means two routes, one per vendor:

* **NVIDIA** - ``nvidia-smi``, which ships with every NVIDIA driver and knows
  the branch version (``581.57``) the CUDA runtimes actually care about.
* **AMD** - the display-adapter registry. There is no ``nvidia-smi`` equivalent
  installed with a consumer Radeon driver, and WMI's ``Win32_VideoController``
  cannot be used: its ``AdapterRAM`` is a 32-bit field, so a 32 GB card is
  reported as ~4 GB. ``HardwareInformation.qwMemorySize`` is 64-bit and right.
"""
from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass

from app.i18n import t

#: torch 2.10 cu128 needs driver R570+, torch 2.8 cu126 needs R560+. NVIDIA only:
#: AMD's Windows driver version (32.0.x) is a Windows build number, not a ROCm
#: one, so there is no honest threshold to compare it against and none is applied.
MIN_DRIVER = 570.0

#: Display adapters live under this device class.
_DISPLAY_CLASS = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"


@dataclass
class GPU:
    name: str
    vram_mib: int
    driver: str
    vendor: str = "nvidia"          # nvidia | amd

    @property
    def vram_gib(self) -> float:
        return self.vram_mib / 1024

    @property
    def driver_number(self) -> float:
        """The NVIDIA branch version as a number; 0.0 for anything else."""
        if self.vendor != "nvidia":
            return 0.0
        try:
            major, _, minor = self.driver.partition(".")
            return float(f"{int(major)}.{int(minor or 0):02d}")
        except ValueError:
            return 0.0


def detect() -> GPU | None:
    """The largest usable card: NVIDIA if there is one, else AMD, else None."""
    return _detect_nvidia() or _detect_amd()


def _detect_nvidia() -> GPU | None:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    try:
        out = subprocess.run(
            [exe, "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    cards = []
    for line in out.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) == 3 and parts[1].isdigit():
            cards.append(GPU(parts[0], int(parts[1]), parts[2], "nvidia"))
    return max(cards, key=lambda g: g.vram_mib) if cards else None


def _detect_amd() -> GPU | None:
    """The largest AMD adapter the display driver has registered.

    Entries without a memory size are virtual adapters (Remote Desktop and the
    like) and are skipped, as is anything from another vendor.
    """
    try:
        import winreg
    except ImportError:              # not Windows; the app is Windows-only anyway
        return None
    cards = []
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _DISPLAY_CLASS) as root:
            for index in range(64):
                try:
                    name = winreg.EnumKey(root, index)
                except OSError:
                    break
                if not name.isdigit():
                    continue
                card = _read_adapter(winreg, root, name)
                if card is not None:
                    cards.append(card)
    except OSError:
        return None
    return max(cards, key=lambda g: g.vram_mib) if cards else None


def _read_adapter(winreg, root, subkey: str) -> GPU | None:
    try:
        with winreg.OpenKey(root, subkey) as key:
            def value(field, default=""):
                try:
                    return winreg.QueryValueEx(key, field)[0]
                except OSError:
                    return default

            provider = str(value("ProviderName"))
            if "advanced micro devices" not in provider.lower() and "amd" not in provider.lower():
                return None
            memory = value("HardwareInformation.qwMemorySize", 0)
            if not isinstance(memory, int) or memory <= 0:
                return None          # virtual adapter, no real memory of its own
            return GPU(str(value("DriverDesc", "AMD GPU")), int(memory // 2**20),
                       str(value("DriverVersion", "")), "amd")
    except OSError:
        return None


#: How hard to lean on the card. The engine always gets the whole card; a mode
#: only switches on savers (see MEMORY_MODES in workers/yue2_worker.py). An
#: earlier design passed "12 GB" as YuE2's memory budget, but that budget is a
#: hard ceiling, so on a 16 GB card it caused the out-of-memory it meant to avoid.
MEMORY_MODES = ("fast", "balanced", "low")


def auto_memory(gpu: GPU | None) -> str:
    """Below 24 GB, Lowest memory. Measured on a 16 GB RTX A4000 with a 3-minute
    song: Balanced peaked at 12.9 GiB (of 14 usable), Lowest memory at 9.5 GiB,
    and Lowest memory was only 4% slower (354 s against 340 s)."""
    if gpu is None or gpu.vram_gib >= 23:
        return "fast"
    return "low"


def verdict(gpu: GPU | None) -> tuple[str, str]:
    """(level, sentence) where level is ok | warn | bad."""
    if gpu is None:
        return "bad", t("No supported graphics card found - Ceonggo needs an NVIDIA or AMD card "
                        "to make music.")
    if gpu.driver_number and gpu.driver_number < MIN_DRIVER:
        return "bad", t("{name}: the graphics driver ({driver}) is too old. Update it from nvidia.com "
                        "to version {need} or newer.", name=gpu.name, driver=gpu.driver,
                        need=int(MIN_DRIVER))
    gib = round(gpu.vram_gib)
    if gpu.vram_gib >= 23:
        return "ok", t("{name} · {gib} GB - full speed.", name=gpu.name, gib=gib)
    if gpu.vram_gib >= 11:
        return "warn", t("{name} · {gib} GB - works in low-memory mode, slower than a 24 GB card.",
                         name=gpu.name, gib=gib)
    return "bad", t("{name} · {gib} GB - not enough graphics memory (12 GB minimum).",
                    name=gpu.name, gib=gib)
