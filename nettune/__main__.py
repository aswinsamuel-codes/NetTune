"""Entry point: ``python -m nettune``."""
from __future__ import annotations

import argparse
import ctypes
import sys
import threading
import webbrowser

from . import __version__
from .engine import Engine
from .server import create_server
from .system import IS_WINDOWS, is_admin


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="nettune", description="Local network diagnosis and optimization dashboard.")
    parser.add_argument("--port", type=int, default=8765, help="local port for the dashboard (default 8765)")
    parser.add_argument("--no-browser", action="store_true", help="do not open the dashboard in a browser")
    parser.add_argument("--takeover", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--version", action="version", version=f"NetTune {__version__}")
    args = parser.parse_args(argv)

    if not IS_WINDOWS:
        print("NetTune currently supports Windows 10 and 11 only. Linux and macOS backends are on the roadmap.")
        return 1

    engine = Engine()
    try:
        server = create_server(engine, args.port, takeover=args.takeover)
    except OSError as exc:
        print(f"NetTune could not start: {exc}")
        return 1

    url = f"http://127.0.0.1:{server.server_address[1]}/"
    rights = "administrator" if is_admin() else "standard user (some fixes need 'Restart as administrator')"
    print(f"NetTune {__version__}")
    print(f"  Dashboard : {url}")
    print(f"  Running as: {rights}")
    print("  Everything stays on this computer. Press Ctrl+C to quit.")

    # Closing the console window skips "finally" blocks, so restore system
    # settings (the Windows proxy, if the combiner set it) from a console handler.
    handler_type = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_uint)

    def on_console_event(event):
        engine.stop()
        return 0  # let Windows continue with its default handling

    console_handler = handler_type(on_console_event)  # keep a reference alive
    ctypes.windll.kernel32.SetConsoleCtrlHandler(console_handler, 1)

    engine.start()
    if not args.no_browser:
        threading.Timer(0.6, webbrowser.open, [url]).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        engine.stop()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
