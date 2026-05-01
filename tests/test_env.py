from pathlib import Path
from unittest.mock import patch

from alchemd import env


def test_venv_bin_windows():
    with patch.object(env, "IS_WINDOWS", True):
        p = env.venv_bin(Path("C:/v"), "pip")
        assert p == Path("C:/v/Scripts/pip.exe")


def test_venv_bin_unix():
    with patch.object(env, "IS_WINDOWS", False):
        p = env.venv_bin(Path("/v"), "pip")
        assert p == Path("/v/bin/pip")


def test_detect_cuda_index_fallback_when_no_smi():
    with patch.object(env, "find_nvidia_smi", return_value=None):
        idx = env.detect_cuda_index()
        assert idx.startswith("https://download.pytorch.org/whl/cu")


def test_find_ghostscript_returns_none_when_absent(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: None)
    monkeypatch.setattr(env, "IS_WINDOWS", False)
    assert env.find_ghostscript() is None
