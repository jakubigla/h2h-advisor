import json
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass, field

import pytest
from playwright.sync_api import Error as PlaywrightError

from h2h_advisor.browser.guard import GuardRules
from h2h_advisor.browser.session import (
    BrowserBusy,
    BrowserSession,
    FetchFailed,
    NeedsLogin,
    ProfileLock,
    detect_auth_problem,
    disable_session_restore,
)
from h2h_advisor.cache import Cache

YAHOO_PAGE = "https://basketball.fantasysports.yahoo.com/nba/123"
BBM_PAGE = "https://basketballmonster.com/dailyprojections.aspx"


# -- profile lock -------------------------------------------------------------


def test_profile_lock_second_holder_busy(tmp_path):
    with ProfileLock(tmp_path), pytest.raises(BrowserBusy):
        ProfileLock(tmp_path).__enter__()


def test_profile_lock_exit_leaves_lock_with_child_that_inherited_it(tmp_path):
    with ProfileLock(tmp_path) as lock:
        child = subprocess.Popen(["sleep", "60"], pass_fds=(lock.fileno(),))
    try:
        # flock(LOCK_UN) would drop the lock for the child too; closing must not.
        with pytest.raises(BrowserBusy):
            ProfileLock(tmp_path).__enter__()
    finally:
        child.kill()
        child.wait()
    with ProfileLock(tmp_path):
        pass


def test_profile_lock_released_after_exception(tmp_path):
    with pytest.raises(RuntimeError), ProfileLock(tmp_path):
        raise RuntimeError("boom")
    with ProfileLock(tmp_path):
        pass
    assert (tmp_path / ".h2h.lock").exists()


# -- auth detection -----------------------------------------------------------


def test_detect_login_redirect():
    assert (
        detect_auth_problem("yahoo", "https://login.yahoo.com/?done=..", "") is not None
    )


def test_detect_bbm_login_path():
    assert (
        detect_auth_problem("bbm", "https://basketballmonster.com/login.aspx", "")
        is not None
    )


def test_detect_captcha():
    html = "<html><body>Please Verify You Are Human</body></html>"
    assert detect_auth_problem("yahoo", YAHOO_PAGE, html) is not None
    assert detect_auth_problem("yahoo", YAHOO_PAGE, "<div id='CAPTCHA'>") is not None


def test_detect_bbm_logged_out_projections():
    html = "<td>Available with a membership</td>"
    assert detect_auth_problem("bbm", BBM_PAGE, html) is not None


def test_clean_page_ok():
    assert detect_auth_problem("yahoo", YAHOO_PAGE, "<html>roster</html>") is None
    assert detect_auth_problem("bbm", BBM_PAGE, "<html>projections</html>") is None


# -- fakes for BrowserSession without a real browser ----------------------------


@dataclass
class FakeFrame:
    parent_frame: "FakeFrame | None" = None
    page: "FakePage | None" = None


@dataclass
class FakeRequest:
    url: str
    method: str = "GET"
    redirected_from: "FakeRequest | None" = None
    resource_type: str = "document"
    post_data: str | None = None
    navigation: bool = True
    frame: "FakeFrame" = field(default_factory=lambda: FakeFrame())

    def is_navigation_request(self) -> bool:
        return self.navigation


def chain(*hops: tuple[str, str]) -> FakeRequest:
    """Build a redirect chain from (method, url) hops; returns the final request."""
    request = None
    for method, url in hops:
        request = FakeRequest(url=url, method=method, redirected_from=request)
    assert request is not None
    return request


@dataclass
class FakeResponse:
    status: int
    request: FakeRequest

    @property
    def url(self) -> str:
        return self.request.url


class FakeNavigation:
    value: FakeResponse


class FakePage:
    """Page stand-in. It has no select_option, so any call to it fails the test."""

    def __init__(self, outcomes, html="<html>ok</html>", url="about:blank"):
        self.outcomes = list(outcomes)
        self.html = html
        self.url = url
        self.gotos: list[str] = []
        self.evaluated: list[tuple[str, object]] = []
        self.navigations = 0
        self.url_after_load: str | None = None  # simulates a meta refresh
        self.closed = False

    def goto(self, url, wait_until=None):
        self.gotos.append(url)
        return self._next(url)

    def _next(self, url):
        outcome = self.outcomes.pop(0)
        if callable(outcome):
            return outcome(url)
        self.url = outcome.request.url
        return outcome

    @contextmanager
    def expect_navigation(self, wait_until=None):
        self.navigations += 1
        nav = FakeNavigation()
        yield nav
        nav.value = self._next(self.url)

    def evaluate(self, script, arg=None):
        self.evaluated.append((script, arg))

    def wait_for_load_state(self, state=None):
        if self.url_after_load is not None:
            self.url = self.url_after_load

    def content(self):
        return self.html

    def close(self):
        self.closed = True


class FakeRoute:
    def __init__(self):
        self.aborted: str | None = None
        self.continued = False

    def abort(self, error_code=None):
        self.aborted = error_code

    def continue_(self):
        self.continued = True


class FakeLimiter:
    def __init__(self):
        self.acquired: list[str] = []
        self.loads: list[tuple[str, str]] = []

    def acquire(self, site):
        self.acquired.append(site)

    def record_load(self, site, url):
        self.loads.append((site, url))


@pytest.fixture
def cache(tmp_path):
    return Cache(tmp_path / "cache.db")


@pytest.fixture
def limiter():
    return FakeLimiter()


@pytest.fixture
def sleeps():
    return []


@pytest.fixture
def make_session(cache, limiter, sleeps):
    def make(page):
        session = BrowserSession(cache, limiter, GuardRules.load(), sleep=sleeps.append)
        session._page = page  # stand-in for a launched browser
        return session

    return make


def ok(url=YAHOO_PAGE, status=200):
    return FakeResponse(status, chain(("GET", url)))


# -- fetch ----------------------------------------------------------------------


def test_fetch_returns_page_result(make_session, limiter):
    session = make_session(FakePage([ok()]))
    result = session.fetch("yahoo", YAHOO_PAGE)
    assert result.url == YAHOO_PAGE
    assert result.final_url == YAHOO_PAGE
    assert result.html == "<html>ok</html>"
    assert limiter.acquired == ["yahoo"]
    assert limiter.loads == [("yahoo", YAHOO_PAGE)]


def test_fetch_safe_same_site_redirect_ok(make_session):
    redirect = FakeResponse(
        200, chain(("GET", YAHOO_PAGE), ("GET", YAHOO_PAGE + "/team"))
    )
    result = make_session(FakePage([redirect])).fetch("yahoo", YAHOO_PAGE)
    assert result.final_url == YAHOO_PAGE + "/team"


def test_fetch_redirect_to_foreign_host_is_unsafe(make_session):
    redirect = FakeResponse(
        200, chain(("GET", YAHOO_PAGE), ("GET", "https://evil.example.com/"))
    )
    with pytest.raises(FetchFailed) as err:
        make_session(FakePage([redirect])).fetch("yahoo", YAHOO_PAGE)
    assert err.value.site == "yahoo"
    assert err.value.status_or_reason == "unsafe redirect"


def test_fetch_redirect_hop_through_action_url_is_unsafe(make_session):
    redirect = FakeResponse(
        200,
        chain(
            ("GET", YAHOO_PAGE),
            ("GET", YAHOO_PAGE + "/dropplayer?pid=1"),
            ("GET", YAHOO_PAGE),
        ),
    )
    with pytest.raises(FetchFailed, match="unsafe redirect"):
        make_session(FakePage([redirect])).fetch("yahoo", YAHOO_PAGE)


def test_fetch_redirect_with_non_get_hop_is_unsafe(make_session):
    redirect = FakeResponse(
        200, chain(("GET", YAHOO_PAGE), ("POST", YAHOO_PAGE + "/team"))
    )
    with pytest.raises(FetchFailed, match="unsafe redirect"):
        make_session(FakePage([redirect])).fetch("yahoo", YAHOO_PAGE)


def test_fetch_redirect_to_login_host_needs_login(make_session):
    redirect = FakeResponse(
        200, chain(("GET", YAHOO_PAGE), ("GET", "https://login.yahoo.com/?done=x"))
    )
    with pytest.raises(NeedsLogin) as err:
        make_session(FakePage([redirect])).fetch("yahoo", YAHOO_PAGE)
    assert err.value.site == "yahoo"


def test_fetch_redirect_to_bbm_login_needs_login(make_session):
    redirect = FakeResponse(
        200,
        chain(("GET", BBM_PAGE), ("GET", "https://basketballmonster.com/login.aspx")),
    )
    with pytest.raises(NeedsLogin):
        make_session(FakePage([redirect])).fetch("bbm", BBM_PAGE)


def test_fetch_captcha_page_needs_login_without_retry(make_session, sleeps):
    page = FakePage([ok()], html="verify you are human")
    with pytest.raises(NeedsLogin):
        make_session(page).fetch("yahoo", YAHOO_PAGE)
    assert sleeps == []


def test_guard_aborted_login_navigation_needs_login(make_session, cache):
    login_url = "https://login.yahoo.com/?done=x"
    holder = {}

    def blocked_redirect(url):
        # The browser routes the login hop, the guard aborts it, goto then fails.
        route = FakeRoute()
        holder["session"]._on_route(route, FakeRequest(url=login_url))
        assert route.aborted == "blockedbyclient"
        raise PlaywrightError(f"net::ERR_BLOCKED_BY_CLIENT at {url}")

    session = make_session(FakePage([blocked_redirect]))
    holder["session"] = session
    with pytest.raises(NeedsLogin) as err:
        session.fetch("yahoo", YAHOO_PAGE)
    assert err.value.reason == "redirected to login"
    assert cache.recent_blocked()[0]["url"] == login_url


def test_guard_aborted_other_navigation_fails(make_session):
    def blocked(url):
        raise PlaywrightError(f"net::ERR_BLOCKED_BY_CLIENT at {url}")

    with pytest.raises(FetchFailed):
        make_session(FakePage([blocked])).fetch("yahoo", YAHOO_PAGE)


@pytest.mark.parametrize("status", [429, 403, 500, 503])
def test_fetch_retries_once_then_fails(make_session, limiter, sleeps, status):
    page = FakePage([ok(status=status), ok(status=status)])
    with pytest.raises(FetchFailed) as err:
        make_session(page).fetch("yahoo", YAHOO_PAGE)
    assert err.value.status_or_reason == status
    assert len(page.gotos) == 2
    assert len(sleeps) == 1 and sleeps[0] >= 30
    assert limiter.acquired == ["yahoo", "yahoo"]


def test_fetch_retry_succeeds(make_session, sleeps):
    page = FakePage([ok(status=429), ok()])
    result = make_session(page).fetch("yahoo", YAHOO_PAGE)
    assert result.final_url == YAHOO_PAGE
    assert len(sleeps) == 1


def test_fetch_404_fails_without_retry(make_session, sleeps):
    with pytest.raises(FetchFailed) as err:
        make_session(FakePage([ok(status=404)])).fetch("yahoo", YAHOO_PAGE)
    assert err.value.status_or_reason == 404
    assert sleeps == []


# -- route handler ----------------------------------------------------------------


def test_route_denied_request_aborted_and_logged(make_session, cache):
    session = make_session(FakePage([]))
    route = FakeRoute()
    session._on_route(
        route,
        FakeRequest(
            url=YAHOO_PAGE + "/x",
            method="POST",
            resource_type="fetch",
            navigation=False,
        ),
    )
    assert route.aborted == "blockedbyclient"
    assert not route.continued
    blocked = cache.recent_blocked()
    assert blocked[0]["site"] == "yahoo"
    assert blocked[0]["method"] == "POST"


def test_route_foreign_host_logged_as_other(make_session, cache):
    route = FakeRoute()
    make_session(FakePage([]))._on_route(
        route, FakeRequest(url="https://evil.example.com/", navigation=False)
    )
    assert route.aborted == "blockedbyclient"
    assert cache.recent_blocked()[0]["site"] == "other"


def test_route_allowed_request_continues(make_session):
    route = FakeRoute()
    make_session(FakePage([]))._on_route(route, FakeRequest(url=YAHOO_PAGE))
    assert route.continued
    assert route.aborted is None


def test_route_handler_error_fails_closed(make_session):
    class BrokenRequest(FakeRequest):
        def is_navigation_request(self):
            raise RuntimeError("boom")

    route = FakeRoute()
    make_session(FakePage([]))._on_route(route, BrokenRequest(url=YAHOO_PAGE))
    assert route.aborted is not None
    assert not route.continued


# -- download ---------------------------------------------------------------------


def test_download_rejects_unlisted_selector(make_session):
    with pytest.raises(ValueError):
        make_session(FakePage([])).download("bbm", BBM_PAGE, "#delete-account")


# -- session lifecycle --------------------------------------------------------------


def test_session_takes_lock_before_launch_and_releases_on_failure(
    home, cache, limiter, monkeypatch
):
    from h2h_advisor import paths
    from h2h_advisor.browser import session as session_module

    def failing_launch():
        # The lock must already be held when the browser is started.
        with pytest.raises(BrowserBusy):
            ProfileLock(paths.profile_dir()).__enter__()
        raise RuntimeError("chrome failed to start")

    monkeypatch.setattr(session_module, "sync_playwright", failing_launch)
    with (
        pytest.raises(RuntimeError, match="chrome failed"),
        BrowserSession(cache, limiter, GuardRules.load()),
    ):
        pass
    with ProfileLock(paths.profile_dir()):
        pass


def test_session_busy_when_profile_locked(home, cache, limiter, monkeypatch):
    from h2h_advisor import paths
    from h2h_advisor.browser import session as session_module

    def must_not_launch():
        raise AssertionError("launched while another session holds the profile")

    monkeypatch.setattr(session_module, "sync_playwright", must_not_launch)
    with (
        ProfileLock(paths.profile_dir()),
        pytest.raises(BrowserBusy),
        BrowserSession(cache, limiter, GuardRules.load()),
    ):
        pass


# -- postback / unexpected navigation / popups ----------------------------------------


def test_postback_sets_selects_without_events_and_loads_once(make_session, limiter):
    page = FakePage([ok(BBM_PAGE)], url=BBM_PAGE)
    session = make_session(page)
    result = session.postback(
        "bbm", BBM_PAGE, "PlayerFilterControl", select={"GAMEDATE": "11/25/2026"}
    )
    assert result.final_url == BBM_PAGE
    # One field assignment, then exactly one __doPostBack inside one navigation.
    field_calls = [
        arg for _, arg in page.evaluated if arg == ["GAMEDATE", "11/25/2026"]
    ]
    assert len(field_calls) == 1
    assert "dispatchEvent" not in page.evaluated[0][0]
    assert page.evaluated[1][1] == "PlayerFilterControl"
    assert page.navigations == 1
    assert limiter.acquired == ["bbm"]
    assert limiter.loads == [("bbm", BBM_PAGE)]


@pytest.mark.parametrize(
    ("event_target", "select"),
    [
        ("dcsave_DisplayColumnsId", None),
        ("TeamFilterControl", None),
        ("playerfiltercontrol", None),  # target match is exact-case
        ("", None),
        ("PlayerFilterControl", {"CLEARGAMES": "x"}),
        ("PlayerFilterControl", {"cvadd_CustomValuesService": "x"}),
        ("PlayerFilterControl", {"TeamFilterControl": "LAL"}),
        ("PlayerFilterControl", {"GAMEDATE": "11/25/2026", "G1234": "on"}),
        ("PlayerFilterControl", {"__VIEWSTATE": "forged"}),
    ],
)
def test_postback_refuses_unapproved_target_or_field_before_touching_page(
    make_session, limiter, event_target, select
):
    page = FakePage([ok(BBM_PAGE)], url="about:blank")
    with pytest.raises(FetchFailed, match="postback"):
        make_session(page).postback("bbm", BBM_PAGE, event_target, select=select)
    assert page.gotos == []
    assert page.evaluated == []
    assert page.navigations == 0
    assert limiter.acquired == []


def test_postback_settable_field_match_is_case_insensitive(make_session):
    page = FakePage([ok(BBM_PAGE)], url=BBM_PAGE)
    make_session(page).postback(
        "bbm", BBM_PAGE, "pageTitleRowLeague", select={"PAGETITLEROWLEAGUE": "602"}
    )
    assert page.navigations == 1


def test_postback_unsafe_redirect_detected(make_session):
    redirect = FakeResponse(
        200, chain(("POST", BBM_PAGE), ("GET", "https://evil.example.com/"))
    )
    page = FakePage([redirect], url=BBM_PAGE)
    with pytest.raises(FetchFailed, match="unsafe redirect"):
        make_session(page).postback("bbm", BBM_PAGE, "PlayerFilterControl")


def test_fetch_page_navigating_on_its_own_fails(make_session):
    page = FakePage([ok()])
    page.url_after_load = YAHOO_PAGE + "/elsewhere"
    with pytest.raises(FetchFailed) as err:
        make_session(page).fetch("yahoo", YAHOO_PAGE)
    assert err.value.status_or_reason == "unexpected navigation"


def test_popup_closed_and_logged(make_session, cache):
    session = make_session(FakePage([]))
    popup = FakePage([], url="https://basketball.fantasysports.yahoo.com/popup")
    session._on_new_page(popup)
    assert popup.closed
    blocked = cache.recent_blocked()[0]
    assert blocked["reason"] == "deny: popup closed"
    assert blocked["url"] == popup.url


def test_own_page_event_not_closed(make_session, cache):
    page = FakePage([])
    session = make_session(page)
    session._on_new_page(page)
    assert not page.closed
    assert cache.recent_blocked() == []


def test_blocked_subframe_navigation_not_taken_for_main(make_session):
    login_url = "https://login.yahoo.com/?done=x"
    holder = {}

    def blocked(url):
        iframe = FakeRequest(url=login_url, frame=FakeFrame(parent_frame=FakeFrame()))
        holder["session"]._on_route(FakeRoute(), iframe)
        raise PlaywrightError(f"net::ERR_BLOCKED_BY_CLIENT at {url}")

    session = make_session(FakePage([blocked]))
    holder["session"] = session
    with pytest.raises(FetchFailed, match="blocked by guard"):
        session.fetch("yahoo", YAHOO_PAGE)


# -- Chrome preferences -------------------------------------------------------------


def test_disable_session_restore_creates_preferences(tmp_path):
    disable_session_restore(tmp_path)
    prefs = json.loads((tmp_path / "Default" / "Preferences").read_text())
    assert prefs["session"] == {"restore_on_startup": 5, "startup_urls": []}


def test_disable_session_restore_merges_existing(tmp_path):
    prefs_path = tmp_path / "Default" / "Preferences"
    prefs_path.parent.mkdir()
    prefs_path.write_text(
        json.dumps(
            {
                "profile": {"name": "h2h"},
                "session": {
                    "restore_on_startup": 1,
                    "startup_urls": ["https://x/"],
                    "other": True,
                },
            }
        )
    )
    disable_session_restore(tmp_path)
    prefs = json.loads(prefs_path.read_text())
    assert prefs["profile"] == {"name": "h2h"}
    assert prefs["session"] == {
        "restore_on_startup": 5,
        "startup_urls": [],
        "other": True,
    }


# -- launch options -----------------------------------------------------------------


class FakeLaunchedContext:
    def __init__(self):
        self.pages = [FakePage([ok("about:blank")])]

    def route(self, pattern, handler):
        pass

    def route_web_socket(self, pattern, handler):
        pass

    def on(self, event, handler):
        pass

    def close(self):
        pass


class FakePlaywright:
    def __init__(self):
        self.launches: list[tuple[str, dict]] = []
        self.chromium = self

    def start(self):
        return self

    def stop(self):
        pass

    def launch_persistent_context(self, user_data_dir, **options):
        self.launches.append((user_data_dir, options))
        return FakeLaunchedContext()


def test_launch_keeps_real_keychain_so_plain_chrome_cookies_decrypt(
    home, monkeypatch, cache, limiter
):
    fake = FakePlaywright()
    monkeypatch.setattr("h2h_advisor.browser.session.sync_playwright", lambda: fake)
    with BrowserSession(cache, limiter, GuardRules.load()):
        pass
    [(_, options)] = fake.launches
    assert options["ignore_default_args"] == ["--use-mock-keychain"]
    assert options["channel"] == "chrome"
    assert options["service_workers"] == "block"
