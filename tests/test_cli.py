import contextlib
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time

import pytest
from playwright.sync_api import Error as PlaywrightError

from h2h_advisor import cli, paths
from h2h_advisor.browser.session import BrowserBusy, ProfileLock
from h2h_advisor.cli import main, wait_for_enter


class PumpedPage:
    """Counts pump ticks; releases the fake Enter key after ``enter_after`` ticks."""

    def __init__(self, enter_after=None, close_after=None, error_after=None):
        self.ticks = 0
        self.enter = threading.Event()
        self.enter_after = enter_after
        self.close_after = close_after
        self.error_after = error_after

    def is_closed(self):
        return self.close_after is not None and self.ticks >= self.close_after

    def wait_for_timeout(self, ms):
        assert ms > 0
        self.ticks += 1
        if self.error_after is not None and self.ticks >= self.error_after:
            raise PlaywrightError("Target closed")
        if self.enter_after is not None and self.ticks >= self.enter_after:
            self.enter.set()


def test_wait_for_enter_pumps_until_enter():
    page = PumpedPage(enter_after=3)
    prompts = []

    def read_line(prompt):
        prompts.append(prompt)
        # Enter only arrives once the main thread has pumped Playwright a few times.
        assert page.enter.wait(timeout=5)

    assert wait_for_enter(page, "press Enter", read_line) is True
    assert page.ticks >= 3
    assert prompts == ["press Enter"]


def test_wait_for_enter_stops_when_page_closes():
    page = PumpedPage(close_after=2)
    blocker = threading.Event()
    assert wait_for_enter(page, "x", lambda _: blocker.wait(5)) is False
    blocker.set()


def test_wait_for_enter_stops_when_browser_errors():
    page = PumpedPage(error_after=1)
    blocker = threading.Event()
    assert wait_for_enter(page, "x", lambda _: blocker.wait(5)) is False
    blocker.set()


def test_wait_for_enter_eof_counts_as_enter():
    def no_stdin(_):
        raise EOFError

    page = PumpedPage()
    assert wait_for_enter(page, "x", no_stdin) is True


def test_login_rejects_headless():
    with pytest.raises(SystemExit):
        main(["login", "--headless"])


def test_serve_runs_the_mcp_server(monkeypatch):
    from h2h_advisor import server

    ran = []
    monkeypatch.setattr(server, "run", lambda: ran.append(True))
    assert main(["serve"]) == 0
    assert ran == [True]


# -- login via a plain Chrome process ----------------------------------------------

LOCK_FD = 99
REAL_POPEN = subprocess.Popen  # tests patch cli.subprocess.Popen


class FakeChrome:
    """Popen stand-in, running for ``checks`` liveness checks (poll / timed wait)."""

    def __init__(
        self, argv, checks=None, ignores_sigterm=False, exit_code=0, events=None
    ):
        self.argv = argv
        self.events = events if events is not None else []
        self.checks = checks  # None: runs until told to stop
        self.ignores_sigterm = ignores_sigterm
        self.exit_code = exit_code
        self.returncode = None
        self.calls: list[str] = []

    def _check(self):
        if self.returncode is None and self.checks is not None:
            if self.checks <= 0:
                self.returncode = self.exit_code
            self.checks -= 1
        return self.returncode

    def poll(self):
        return self._check()

    def wait(self, timeout=None):
        self.calls.append(f"wait({timeout})")
        if self._check() is None:
            if timeout is not None:
                raise subprocess.TimeoutExpired(self.argv, timeout)
            self.returncode = self.exit_code
        return self.returncode

    def terminate(self):
        self.calls.append("terminate")
        self.events.append("chrome terminated")
        if not self.ignores_sigterm:
            self.returncode = -15

    def kill(self):
        self.calls.append("kill")
        self.returncode = -9


class FakeLock:
    def __init__(self, events, busy=False):
        self.events = events
        self.busy = busy

    def __call__(self, profile_dir):
        self.events.append(("lock", profile_dir))
        return self

    def __enter__(self):
        if self.busy:
            raise BrowserBusy()
        self.events.append("locked")
        return self

    def __exit__(self, *exc):
        self.events.append("released")

    def fileno(self):
        return LOCK_FD


@pytest.fixture
def chrome_bin(tmp_path, monkeypatch):
    binary = tmp_path / "Google Chrome"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    monkeypatch.setenv("H2H_ADVISOR_CHROME", str(binary))
    return binary


@pytest.fixture
def login_env(home, chrome_bin, monkeypatch):
    events: list = []
    launched: list[FakeChrome] = []
    # Default: survives the startup grace check, then runs until stopped.
    state = {"checks": None, "ignores_sigterm": False, "exit_code": 0, "kwargs": None}

    def popen(argv, **kwargs):
        prefs = paths.profile_dir() / "Default" / "Preferences"
        events.append(("chrome started", prefs.exists()))
        state["kwargs"] = kwargs
        proc = FakeChrome(
            argv,
            state["checks"],
            state["ignores_sigterm"],
            state["exit_code"],
            events,
        )
        launched.append(proc)
        return proc

    monkeypatch.setattr(cli, "ProfileLock", FakeLock(events))
    monkeypatch.setattr(cli.subprocess, "Popen", popen)
    return events, launched, state


def test_login_tty_enter_terminates_chrome(login_env, capsys):
    events, launched, _ = login_env
    assert cli.login(stdin_is_tty=True, read_line=lambda _: None) == 0
    [proc] = launched
    assert proc.calls == [
        f"wait({cli.CHROME_STARTUP_GRACE_S})",
        "terminate",
        f"wait({cli.CHROME_QUIT_TIMEOUT_S})",
    ]
    assert events[-1] == "released"
    out = capsys.readouterr().out
    assert "press Enter here (or quit that Chrome window with Cmd+Q)" in out
    assert "independent" in out
    assert "Login session saved" in out


def test_login_tty_kills_chrome_that_ignores_sigterm(login_env, capsys):
    _, launched, state = login_env
    state["ignores_sigterm"] = True
    assert cli.login(stdin_is_tty=True, read_line=lambda _: None) == 0
    assert launched[0].calls[1] == "terminate"
    assert "kill" in launched[0].calls
    assert "cookies may not have been saved" in capsys.readouterr().err


def test_login_unexpected_error_quits_chrome_before_releasing_lock(
    login_env, monkeypatch
):
    events, launched, _ = login_env

    def broken(*args):
        raise OSError("stdout gone")

    monkeypatch.setattr(cli, "_supervise", broken)
    with pytest.raises(OSError, match="stdout gone"):
        cli.login(stdin_is_tty=False)
    assert launched[0].returncode is not None
    assert events[-2:] == ["chrome terminated", "released"]


def test_login_tty_chrome_quit_before_enter(login_env):
    events, launched, state = login_env
    state["checks"] = 3
    blocker = threading.Event()
    assert cli.login(stdin_is_tty=True, read_line=lambda _: blocker.wait(5)) == 0
    blocker.set()
    assert "terminate" not in launched[0].calls
    assert events[-1] == "released"


def test_login_non_tty_waits_for_chrome_exit(login_env, capsys):
    events, launched, state = login_env
    state["checks"] = 1

    def no_read(_):
        raise AssertionError("must not read stdin without a TTY")

    assert cli.login(stdin_is_tty=False, read_line=no_read) == 0
    [proc] = launched
    assert proc.calls == [f"wait({cli.CHROME_STARTUP_GRACE_S})", "wait(None)"]
    assert events == [
        ("lock", paths.profile_dir()),
        "locked",
        ("chrome started", True),  # Preferences written before Chrome starts
        "released",
    ]
    out = capsys.readouterr().out
    assert "quit that Chrome window with Cmd+Q" in out
    assert "closing the window alone keeps Chrome running" in out


def test_login_argv_is_plain_chrome_on_the_profile(login_env, chrome_bin):
    _, launched, state = login_env
    cli.login(stdin_is_tty=True, read_line=lambda _: None)
    argv = launched[0].argv
    assert argv == [
        str(chrome_bin),
        f"--user-data-dir={paths.profile_dir()}",
        "--no-first-run",
        "--no-default-browser-check",
        "--new-window",
        cli.YAHOO_LOGIN_URL,
        cli.BBM_LOGIN_URL,
    ]
    joined = " ".join(argv)
    for flag in ("remote-debugging", "enable-automation", "headless", "mock-keychain"):
        assert flag not in joined
    kwargs = state["kwargs"]
    assert kwargs["pass_fds"] == (LOCK_FD,)  # Chrome itself holds the profile lock
    for stream in ("stdin", "stdout", "stderr"):
        assert kwargs[stream] is subprocess.DEVNULL


def test_login_disables_session_restore(login_env):
    cli.login(stdin_is_tty=True, read_line=lambda _: None)
    prefs = json.loads((paths.profile_dir() / "Default" / "Preferences").read_text())
    assert prefs["session"]["restore_on_startup"] == 5


def test_login_default_chrome_path(monkeypatch):
    monkeypatch.delenv("H2H_ADVISOR_CHROME", raising=False)
    assert cli.chrome_binary() == cli.Path(cli.CHROME_BIN)
    assert cli.CHROME_BIN == (
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    )


def test_login_chrome_exiting_at_once_is_an_error(login_env, capsys):
    events, _, state = login_env
    state["checks"] = 0
    assert cli.login(stdin_is_tty=False) == 1
    assert "already open" in capsys.readouterr().err
    assert events[-1] == "released"


def test_login_chrome_failing_exit_status_is_an_error(login_env, capsys):
    _, _, state = login_env
    state["checks"], state["exit_code"] = 1, 3
    assert cli.login(stdin_is_tty=False) == 1
    captured = capsys.readouterr()
    assert "status 3" in captured.err
    assert "Login session saved" not in captured.out


def test_login_refuses_profile_open_in_running_chrome(login_env, capsys):
    events, launched, _ = login_env
    profile = paths.profile_dir()
    profile.mkdir(parents=True)
    (profile / "SingletonLock").symlink_to(f"{socket.gethostname()}-{os.getpid()}")
    assert cli.login(stdin_is_tty=False) == 1
    assert launched == []
    assert "That Chrome profile is already open" in capsys.readouterr().err
    assert events[-1] == "released"


@pytest.mark.parametrize("target", ["{host}-{dead}", "garbage", "{host}-notapid"])
def test_login_ignores_stale_singleton_lock(login_env, target):
    _, launched, state = login_env
    state["checks"] = 1
    reaped = REAL_POPEN(["true"])
    reaped.wait()
    profile = paths.profile_dir()
    profile.mkdir(parents=True)
    (profile / "SingletonLock").symlink_to(
        target.format(host=socket.gethostname(), dead=reaped.pid)
    )
    assert cli.login(stdin_is_tty=False) == 0
    assert len(launched) == 1


def test_login_popen_oserror_exits_1(login_env, monkeypatch, capsys):
    events, _, _ = login_env

    def broken(argv, **kwargs):
        raise PermissionError("denied")

    monkeypatch.setattr(cli.subprocess, "Popen", broken)
    assert cli.login(stdin_is_tty=False) == 1
    assert "could not start Google Chrome" in capsys.readouterr().err
    assert events[-1] == "released"


def test_login_lock_busy_exits_1(home, chrome_bin, monkeypatch, capsys):
    events: list = []
    monkeypatch.setattr(cli, "ProfileLock", FakeLock(events, busy=True))
    monkeypatch.setattr(
        cli.subprocess, "Popen", lambda *a, **k: pytest.fail("Chrome must not start")
    )
    assert main(["login"]) == 1
    assert "busy" in capsys.readouterr().err


def test_login_missing_chrome_exits_1(home, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("H2H_ADVISOR_CHROME", str(tmp_path / "no-such-chrome"))
    monkeypatch.setattr(cli, "ProfileLock", FakeLock([]))
    monkeypatch.setattr(
        cli.subprocess, "Popen", lambda *a, **k: pytest.fail("Chrome must not start")
    )
    assert main(["login"]) == 1
    err = capsys.readouterr().err
    assert "no-such-chrome" in err
    assert "H2H_ADVISOR_CHROME" in err


# -- login with real processes (a shell script stands in for Chrome) -----------------

LOGIN_SCRIPT = "import sys; from h2h_advisor.cli import main; sys.exit(main(['login']))"


@pytest.fixture
def real_login(tmp_path):
    """Start ``h2h-advisor login`` in a child Python with a fake Chrome that sleeps."""
    pidfile = tmp_path / "chrome.pid"
    chrome = tmp_path / "fake-chrome"
    chrome.write_text(f'#!/bin/sh\necho $$ > "{pidfile}"\nexec sleep 60\n')
    chrome.chmod(0o755)
    env = {
        **os.environ,
        "H2H_ADVISOR_HOME": str(tmp_path),
        "H2H_ADVISOR_CHROME": str(chrome),
    }
    started: list[subprocess.Popen] = []

    def start():
        proc = subprocess.Popen(
            [sys.executable, "-c", LOGIN_SCRIPT],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        started.append(proc)
        deadline = time.monotonic() + 15
        while not (pidfile.exists() and pidfile.read_text().strip()):
            assert proc.poll() is None, proc.communicate()
            assert time.monotonic() < deadline, "fake Chrome never started"
            time.sleep(0.05)
        return proc, int(pidfile.read_text())

    yield start
    for proc in started:
        if proc.poll() is None:
            proc.kill()
        proc.communicate()
    if pidfile.exists() and pidfile.read_text().strip():
        with contextlib.suppress(ProcessLookupError):
            os.kill(int(pidfile.read_text()), signal.SIGKILL)


def _lock_is_free(profile):
    try:
        with ProfileLock(profile):
            return True
    except BrowserBusy:
        return False


def _wait_gone(pid, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


def test_chrome_keeps_profile_lock_after_login_process_is_killed(real_login, tmp_path):
    login_proc, chrome_pid = real_login()
    profile = tmp_path / ".local" / "share" / "h2h-advisor" / "chrome-profile"
    assert not _lock_is_free(profile)
    login_proc.kill()  # SIGKILL: no handler, no cleanup
    login_proc.wait()
    assert not _lock_is_free(profile)  # the "Chrome" child still holds the flock
    os.kill(chrome_pid, signal.SIGKILL)
    assert _wait_gone(chrome_pid)
    assert _lock_is_free(profile)


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGHUP, signal.SIGINT])
def test_login_signal_quits_chrome_cleanly(real_login, tmp_path, signum):
    login_proc, chrome_pid = real_login()
    login_proc.send_signal(signum)
    _, err = login_proc.communicate(timeout=20)
    assert login_proc.returncode == 1
    assert "Traceback" not in err
    assert "interrupted" in err.lower()
    assert _wait_gone(chrome_pid)
    profile = tmp_path / ".local" / "share" / "h2h-advisor" / "chrome-profile"
    assert _lock_is_free(profile)
