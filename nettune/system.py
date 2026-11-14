"""Thin wrappers around the Windows command line tools the engine relies on.

Everything that touches the operating system goes through this module, so it is
the one place to look at when porting NetTune to another platform.

Security note: values that come from outside the program (SSIDs, adapter names)
are never pasted into a PowerShell script. They are handed over as environment
variables (``NT_<NAME>``) and read inside the script as ``$env:NT_<NAME>``.
"""
from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys

IS_WINDOWS = sys.platform == "win32"
_NO_WINDOW = 0x08000000 if IS_WINDOWS else 0

_PS_PRELUDE = (
    "$ErrorActionPreference='Stop';"
    "$ProgressPreference='SilentlyContinue';"
    "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
)


class CommandError(RuntimeError):
    """A system command failed or could not be run."""


def is_admin() -> bool:
    if not IS_WINDOWS:
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _decode(raw: bytes, encoding: str) -> str:
    try:
        return raw.decode(encoding)
    except (UnicodeDecodeError, LookupError):
        return raw.decode("utf-8", "replace")


def run_cmd(args: list[str], timeout: float = 30) -> tuple[int, str]:
    """Run a console program (netsh, ping, powercfg ...). Returns (exit code, output)."""
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            creationflags=_NO_WINDOW,
        )
    except FileNotFoundError as exc:
        raise CommandError(f"{args[0]} was not found on this system") from exc
    except subprocess.TimeoutExpired as exc:
        raise CommandError(f"{args[0]} did not finish within {timeout:.0f}s") from exc
    out = _decode(proc.stdout, "oem")
    err = _decode(proc.stderr, "oem").strip()
    return proc.returncode, out + ("\n" + err if err else "")


def run_netsh_utf8(command: str, timeout: float = 20) -> tuple[int, str]:
    """Run a *fixed* netsh command with UTF-8 output, so non-ASCII SSIDs survive.

    netsh writes in the console code page; switching it to 65001 first is the
    only way to get names like "Sam’s iPhone" back intact. ``command`` goes
    through cmd.exe, so it must be a constant from this code base - never pass
    anything that came from outside the program.
    """
    if not all(ch.isalnum() or ch in " =-" for ch in command):
        raise CommandError("run_netsh_utf8 only accepts fixed, simple commands")
    try:
        proc = subprocess.run(
            ["cmd.exe", "/d", "/c", f"chcp 65001>nul & netsh {command}"],
            capture_output=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            creationflags=_NO_WINDOW,
        )
    except subprocess.TimeoutExpired as exc:
        raise CommandError(f"netsh did not finish within {timeout:.0f}s") from exc
    return proc.returncode, _decode(proc.stdout, "utf-8") + _decode(proc.stderr, "utf-8")


def run_ps(script: str, args: dict | None = None, timeout: float = 40) -> str:
    """Run a PowerShell snippet and return its stdout.

    ``args`` are exposed to the script as ``$env:NT_<KEY>`` (upper-cased).
    """
    env = os.environ.copy()
    for key, value in (args or {}).items():
        env["NT_" + key.upper()] = str(value)
    wrapped = (
        _PS_PRELUDE
        + "try{& {\n"
        + script
        + "\n}}catch{[Console]::Error.WriteLine($_.Exception.Message);exit 1}"
    )
    try:
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", wrapped],
            capture_output=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            creationflags=_NO_WINDOW,
            env=env,
        )
    except FileNotFoundError as exc:
        raise CommandError("powershell.exe was not found on this system") from exc
    except subprocess.TimeoutExpired as exc:
        raise CommandError(f"PowerShell did not finish within {timeout:.0f}s") from exc
    out = _decode(proc.stdout, "utf-8").strip()
    if proc.returncode != 0:
        err = _decode(proc.stderr, "utf-8").strip() or out or "PowerShell command failed"
        raise CommandError(err.splitlines()[0] if err else "PowerShell command failed")
    return out


def run_ps_json(script: str, args: dict | None = None, timeout: float = 40):
    out = run_ps(script, args, timeout)
    if not out:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError as exc:
        raise CommandError(f"Unexpected PowerShell output: {out[:200]}") from exc


def as_list(value) -> list:
    """Normalise the many shapes PowerShell 5.1 gives a JSON array."""
    if value is None:
        return []
    if isinstance(value, dict) and "value" in value:
        return as_list(value["value"])
    if isinstance(value, list):
        return value
    return [value]
