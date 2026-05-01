"""ANSI colour output, live timer, yes/no prompt. Port of v2 UI helpers."""
from __future__ import annotations

import os
import platform
import sys
import threading
import time

IS_WINDOWS = platform.system() == "Windows"

if IS_WINDOWS:
    os.system("")
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:
    pass

USE_COLOUR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _c(code: str) -> str:
    return code if USE_COLOUR else ""


GREEN, YELLOW, RED, CYAN, BOLD, RESET = (
    _c("\033[92m"), _c("\033[93m"), _c("\033[91m"),
    _c("\033[96m"), _c("\033[1m"), _c("\033[0m"),
)


def ok(msg: str) -> None: print(f"  {GREEN}OK{RESET}    {msg}")
def warn(msg: str) -> None: print(f"  {YELLOW}WARN{RESET}  {msg}")
def err(msg: str) -> None: print(f"  {RED}FAIL{RESET}  {msg}")
def info(msg: str) -> None: print(f"  {CYAN}--{RESET}    {msg}")


def timer_thread(stop_event: threading.Event, label: str) -> None:
    start = time.time()
    printed = False
    while not stop_event.is_set():
        secs = int(time.time() - start)
        m, s = divmod(secs, 60)
        print(f"\r  {CYAN}>>{RESET}   {label}  {m:02d}:{s:02d}", end="", flush=True)
        printed = True
        time.sleep(1)
    if printed:
        print()


def confirm(prompt: str, auto_yes: bool, default: bool = False) -> bool:
    if auto_yes:
        info(f"{prompt}  -> auto-yes")
        return True
    if not sys.stdin.isatty():
        info(f"{prompt}  -> non-interactive, default={default}")
        return default
    answer = input(f"       {prompt} [y/n]: ").strip().lower()
    return answer == "y"
