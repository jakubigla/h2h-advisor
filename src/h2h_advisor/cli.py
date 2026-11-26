"""Command line entry point: ``serve``, ``login`` and ``discover``.

``login`` starts an ordinary Google Chrome (no Playwright, no automation or
remote-debugging flags) on the dedicated profile so a human can sign in; Google
refuses sign-in in automation-controlled browsers. Afterwards the guarded
Playwright session reuses the saved cookies. ``discover`` captures raw pages for
building parsers (Task 12).
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType
from typing import Any

from playwright.sync_api import Error as PlaywrightError

from h2h_advisor import paths
from h2h_advisor.browser.guard import GuardRules, site_of
from h2h_advisor.browser.ratelimit import RateLimiter
from h2h_advisor.browser.session import (
    BrowserBusy,
    BrowserSession,
    FetchFailed,
    NeedsLogin,
    ProfileLock,
    disable_session_restore,
)
from h2h_advisor.cache import Cache
from h2h_advisor.config import load_config

YAHOO_LOGIN_URL = "https://basketball.fantasysports.yahoo.com/"
BBM_LOGIN_URL = "https://basketballmonster.com/login.aspx"
CHROME_BIN = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
CHROME_ENV = "H2H_ADVISOR_CHROME"
# Time Chrome gets after SIGTERM to flush cookies to disk before it is killed.
CHROME_QUIT_TIMEOUT_S = 15
CHROME_POLL_INTERVAL_S = 0.2
# Chrome exiting this soon means it handed off to a Chrome already on the profile
# (or could not start at all).
CHROME_STARTUP_GRACE_S = 3
PROFILE_IN_USE = "That Chrome profile is already open — quit it (Cmd+Q) and rerun"
QUIT_SIGNALS = (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)
RAW_FIXTURES_DIR = Path("tests/fixtures/raw")
PUMP_INTERVAL_MS = 200


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="h2h-advisor")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve", help="run the MCP server")
    # Always headed: a human has to type credentials.
    sub.add_parser("login", help="log in to Yahoo and Basketball Monster")
    discover = sub.add_parser("discover", help="save a page's HTML for parser work")
    discover.add_argument("url")
    discover.add_argument("--headless", action="store_true")
    return parser


def _session(headless: bool) -> tuple[Cache, BrowserSession]:
    cache = Cache(paths.cache_db())
    limiter = RateLimiter(cache, load_config().limits)
    session = BrowserSession(
        cache, limiter, GuardRules.load(), headless=headless, minimized=False
    )
    return cache, session


def wait_for_enter(
    page: Any, prompt: str, read_line: Callable[[str], object] = input
) -> bool:
    """Wait for Enter while keeping Playwright's event loop running.

    The sync API only dispatches route callbacks while Python is inside a
    Playwright call, so a bare ``input()`` would freeze every request the user
    makes in the window. The line is read on a thread while this thread pumps
    ``wait_for_timeout``. Returns False if the page or browser closed first.
    """
    entered = threading.Event()

    def reader() -> None:
        try:
            read_line(prompt)
        except (EOFError, OSError):
            pass
        finally:
            entered.set()

    threading.Thread(target=reader, daemon=True).start()
    while not entered.is_set():
        if page.is_closed():
            return False
        try:
            page.wait_for_timeout(PUMP_INTERVAL_MS)
        except PlaywrightError:
            return False
    return True


def chrome_binary() -> Path:
    return Path(os.environ.get(CHROME_ENV) or CHROME_BIN)


def _wait_for_enter_or_exit(
    proc: subprocess.Popen[bytes], read_line: Callable[[str], object]
) -> bool:
    """True if Enter (or EOF) came first, False if Chrome exited first."""
    entered = threading.Event()

    def reader() -> None:
        try:
            read_line("")
        except (EOFError, OSError):
            pass
        finally:
            entered.set()

    threading.Thread(target=reader, daemon=True).start()
    while proc.poll() is None:
        if entered.wait(CHROME_POLL_INTERVAL_S):
            return True
    return False


def _quit_chrome(proc: subprocess.Popen[bytes]) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=CHROME_QUIT_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        print(
            f"Chrome ignored SIGTERM for {CHROME_QUIT_TIMEOUT_S} s and was killed; "
            "cookies may not have been saved.",
            file=sys.stderr,
        )


class _LoginInterrupted(Exception):
    pass


def _on_quit_signal(signum: int, frame: FrameType | None) -> None:
    for sig in QUIT_SIGNALS:  # a second signal must not interrupt the cleanup
        signal.signal(sig, signal.SIG_IGN)
    raise _LoginInterrupted(signal.Signals(signum).name)


def _profile_in_use(profile: Path) -> bool:
    """A live process owns Chrome's ``SingletonLock`` (symlink to "<host>-<pid>")."""
    try:
        target = os.readlink(profile / "SingletonLock")
    except OSError:
        return False
    pid_text = target.rpartition("-")[2]
    if not pid_text.isdigit():
        return False
    try:
        os.kill(int(pid_text), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _supervise(
    proc: subprocess.Popen[bytes],
    stdin_is_tty: bool,
    read_line: Callable[[str], object],
) -> int:
    try:
        proc.wait(timeout=CHROME_STARTUP_GRACE_S)
    except subprocess.TimeoutExpired:
        pass
    else:
        print(
            f"{PROFILE_IN_USE} (or Chrome failed to start; status {proc.returncode}).",
            file=sys.stderr,
        )
        return 1
    if stdin_is_tty:
        print(
            "Log into Yahoo Fantasy and Basketball Monster in the Chrome window, "
            "then press Enter here (or quit that Chrome window with Cmd+Q)."
        )
        if _wait_for_enter_or_exit(proc, read_line):
            _quit_chrome(proc)
            return 0
    else:
        print(
            "Log into Yahoo Fantasy and Basketball Monster in the Chrome window, "
            "then quit that Chrome window with Cmd+Q (closing the window alone "
            "keeps Chrome running on macOS)."
        )
        proc.wait()
    if proc.returncode != 0:
        print(f"Chrome exited with status {proc.returncode}.", file=sys.stderr)
        return 1
    return 0


def login(
    stdin_is_tty: bool | None = None, read_line: Callable[[str], object] = input
) -> int:
    """Open a plain Chrome on the profile and wait until the human is done."""
    binary = chrome_binary()
    if not (binary.is_file() and os.access(binary, os.X_OK)):
        print(
            f"Google Chrome not found at {binary} (set {CHROME_ENV} to its binary)",
            file=sys.stderr,
        )
        return 1
    if stdin_is_tty is None:
        stdin_is_tty = sys.stdin.isatty()
    profile = paths.profile_dir()
    with ProfileLock(profile) as lock:
        if _profile_in_use(profile):
            print(PROFILE_IN_USE, file=sys.stderr)
            return 1
        disable_session_restore(profile)
        argv = [
            str(binary),
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "--new-window",
            YAHOO_LOGIN_URL,
            BBM_LOGIN_URL,
        ]
        previous = {sig: signal.signal(sig, _on_quit_signal) for sig in QUIT_SIGNALS}
        try:
            try:
                # Chrome inherits the locked fd, so the profile stays locked for
                # Chrome's whole life even if this process dies. Its own session
                # keeps terminal signals away; ours are forwarded by _quit_chrome.
                proc = subprocess.Popen(
                    argv,
                    pass_fds=(lock.fileno(),),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            except OSError as exc:
                print(
                    f"could not start Google Chrome at {binary}: {exc}", file=sys.stderr
                )
                return 1
            try:
                print(
                    "Opened a separate Chrome on the h2h-advisor profile; it runs as "
                    "an independent instance even if your normal Chrome is open."
                )
                status = _supervise(proc, stdin_is_tty, read_line)
            except _LoginInterrupted as exc:
                _quit_chrome(proc)
                print(f"login interrupted ({exc}); Chrome closed.", file=sys.stderr)
                return 1
            except BaseException:
                # Never leave Chrome running once this process stops supervising it.
                if proc.poll() is None:
                    _quit_chrome(proc)
                raise
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
    if status == 0:
        print("Login session saved to the profile.")
    return status


def _discover(url: str, headless: bool) -> int:
    rules = GuardRules.load()
    site = site_of(url, rules)
    if site is None:
        print(f"not a Yahoo or Basketball Monster page: {url}", file=sys.stderr)
        return 2
    cache, session = _session(headless)
    with session:
        try:
            session.fetch(site, url)
        except (NeedsLogin, FetchFailed) as exc:
            print(exc, file=sys.stderr)
            return 1
        if not wait_for_enter(session.page, "Press Enter to save the page..."):
            print("browser closed before the page was saved", file=sys.stderr)
            return 1
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        RAW_FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
        target = RAW_FIXTURES_DIR / f"{stamp}.html"
        target.write_text(session.page.content(), encoding="utf-8")
    print(f"saved {target}")
    print("recent blocked requests:")
    for entry in cache.recent_blocked():
        print(
            f"  {entry['at']:%H:%M:%S} {entry['site']} {entry['method']} "
            f"{entry['url']} ({entry['reason']})"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "serve":
        from h2h_advisor import server  # imported here: the CLI must start fast

        server.run()
        return 0
    try:
        if args.command == "login":
            return login()
        return _discover(args.url, args.headless)
    except BrowserBusy as exc:
        print(exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
