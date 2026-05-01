"""CUDA/venv detection and Ghostscript installer. Ported from v2 with no fitz dependency."""
from __future__ import annotations

import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

from alchemd import ui

IS_WINDOWS = platform.system() == "Windows"

GS_MANUAL_URL = "https://www.ghostscript.com/releases/gsdnld.html"


def venv_bin(venv: Path, name: str) -> Path:
    if IS_WINDOWS:
        return venv / "Scripts" / f"{name}.exe"
    return venv / "bin" / name


def find_nvidia_smi() -> str | None:
    found = shutil.which("nvidia-smi")
    if found:
        return found
    candidates = (
        [r"C:\Windows\System32\nvidia-smi.exe"] if IS_WINDOWS
        else ["/usr/bin/nvidia-smi", "/usr/local/cuda/bin/nvidia-smi"]
    )
    for c in candidates:
        if Path(c).exists():
            return c
    return None


def detect_cuda_index() -> str:
    smi = find_nvidia_smi()
    fallback = "https://download.pytorch.org/whl/cu128"
    if smi is None:
        return fallback
    try:
        r = subprocess.run([smi], capture_output=True, text=True, timeout=10)
    except (subprocess.SubprocessError, OSError):
        return fallback
    m = re.search(r"CUDA Version:\s+(\d+)\.(\d+)", r.stdout or "")
    if not m:
        return fallback
    major, minor = int(m.group(1)), int(m.group(2))
    if major > 12 or (major == 12 and minor >= 8):
        return "https://download.pytorch.org/whl/cu128"
    if major == 12 and minor >= 6:
        return "https://download.pytorch.org/whl/cu126"
    if major == 12 and minor >= 1:
        return "https://download.pytorch.org/whl/cu121"
    return "https://download.pytorch.org/whl/cu118"


def find_ghostscript() -> str | None:
    for name in ("gswin64c", "gswin32c", "gs"):
        if shutil.which(name):
            return name
    if IS_WINDOWS:
        import glob
        for pat in (r"C:\Program Files\gs\gs*\bin\gswin64c.exe",
                    r"C:\Program Files (x86)\gs\gs*\bin\gswin32c.exe"):
            matches = glob.glob(pat)
            if matches:
                return matches[0]
    return None


def install_ghostscript() -> None:
    os_name = platform.system()
    if os_name == "Windows":
        base = ["winget", "install", "--source", "winget",
                "--accept-source-agreements", "--accept-package-agreements"]
        for cmd in (base + ["-e", "--id", "ArtifexSoftware.GhostScript"],
                    base + ["--name", "Ghostscript", "--moniker", "ghostscript"]):
            try:
                subprocess.run(cmd, check=True)
                return
            except subprocess.CalledProcessError:
                continue
        raise RuntimeError(f"winget failed; install manually: {GS_MANUAL_URL}")
    if os_name == "Darwin":
        subprocess.run(["brew", "install", "ghostscript"], check=True); return
    if os_name == "Linux":
        subprocess.run(["sudo", "apt-get", "update"], check=True)
        subprocess.run(["sudo", "apt-get", "install", "-y", "ghostscript"], check=True)
        return
    raise NotImplementedError(f"Auto-install not supported for {os_name}")


def check(venv: Path, auto_yes: bool) -> None:
    """Verify the venv has torch+CUDA, marker, docling, mineru, and Ghostscript is on PATH."""
    venv_pip = venv_bin(venv, "pip")
    print(f"{ui.BOLD}Environment{ui.RESET}")

    try:
        import torch
        if torch.cuda.is_available():
            ui.ok(f"torch CUDA -- {torch.cuda.get_device_name(0)}")
        else:
            ui.warn("torch is CPU-only; engines will be very slow "
                    "(expect ~6x runtime per page). If a CUDA-capable GPU "
                    "is present, install the matching torch wheel from "
                    "https://pytorch.org/get-started/locally/. Otherwise "
                    "force --engine docling (CPU-friendly) or run on a "
                    "GPU host. Adaptive timeouts auto-scale x6 on CPU.")
    except ImportError:
        ui.err("torch not found -- install into the venv before continuing.")
        sys.exit(1)

    for pkg in ("marker", "docling", "mineru"):
        try:
            __import__(pkg)
        except ImportError:
            ui.err(f"{pkg} not importable -- pip install {pkg}")
            sys.exit(1)
        # mineru is invoked via its CLI, not its Python API. Importable
        # without a PATH entry means the Python package is installed but
        # the console script isn't — the engine will fail at resolve and
        # the engine registry will drop it.
        if pkg == "mineru" and shutil.which("mineru") is None:
            ui.warn("mineru importable but CLI not on PATH -- engine disabled "
                    "(activate the venv that exposes the mineru console script "
                    "to enable scanned-PDF fallback).")
        else:
            ui.ok(f"{pkg} found.")

    gs = find_ghostscript()
    if gs:
        ui.ok(f"Ghostscript found -- {gs}")
    else:
        ui.warn("Ghostscript not found; sanitize will fall back to rasterize-only.")
        if ui.confirm("Attempt to install Ghostscript now?", auto_yes):
            try:
                install_ghostscript()
                if find_ghostscript():
                    ui.ok("Ghostscript installed.")
            except Exception as exc:
                ui.warn(f"Ghostscript install failed: {exc}")
    print()
