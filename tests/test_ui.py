import io
import os
import threading
import time
from contextlib import redirect_stdout

from alchemd import ui


def test_confirm_auto_yes(monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert ui.confirm("proceed?", auto_yes=True) is True


def test_confirm_non_interactive_uses_default(monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert ui.confirm("proceed?", auto_yes=False, default=False) is False
    assert ui.confirm("proceed?", auto_yes=False, default=True) is True


def test_helpers_prefix_correctly(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    buf = io.StringIO()
    with redirect_stdout(buf):
        ui.ok("hi")
        ui.warn("hey")
        ui.err("oh")
        ui.info("fyi")
    out = buf.getvalue()
    assert "OK" in out and "WARN" in out and "FAIL" in out and "--" in out


def test_timer_thread_stops_cleanly():
    stop = threading.Event()
    t = threading.Thread(target=ui.timer_thread, args=(stop, "test"))
    t.start()
    time.sleep(0.2)
    stop.set()
    t.join(timeout=2)
    assert not t.is_alive()
