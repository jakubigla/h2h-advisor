"""Guarded browser session over the dedicated Chrome profile.

Every request any page of the context makes goes through ``guard.decide``;
anything denied is aborted before it leaves the machine and logged. Traffic the
route hook cannot see is shut off: service workers are blocked at launch,
WebSockets are closed, popups are closed, and Chrome is told not to restore old
tabs at startup (they would load before the route hook exists).

Logging in happens outside Playwright, in a plain Chrome on the same profile
(``h2h-advisor login``); this session only reuses the saved cookies.

Redirect hops are a known gap: the route hook only sees the first URL of a
redirect chain, so later hops are *detected after the fact* (by walking the
chain once the navigation finishes) and the call fails; they are not prevented.
"""

from __future__ import annotations

import fcntl
import json
import os
import time
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Self
from urllib.parse import urlsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from h2h_advisor import paths
from h2h_advisor.browser.guard import (
    READ_METHODS,
    GuardRules,
    RequestInfo,
    decide,
    normalize_host,
    site_of,
)
from h2h_advisor.browser.ratelimit import RateLimiter
from h2h_advisor.cache import Cache

LOCK_FILE = ".h2h.lock"
# Matches the verdict Cache.recent_blocked() reads.
BLOCKED_VERDICT = "block"
RETRY_DELAY_S = 30.0
RETRY_STATUSES = frozenset({403, 429})
CAPTCHA_MARKERS = ("captcha", "verify you are human")
BBM_LOGGED_OUT_MARKER = "available with a membership"
LOGIN_REASON = "redirected to login"
# Playwright passes this by default; it makes Chrome encrypt cookies with a fake
# key, so cookies the plain-Chrome login saved (real macOS keychain) would not decrypt.
MOCK_KEYCHAIN_ARG = "--use-mock-keychain"
# Chrome's "open the New Tab page" startup setting: never restore previous tabs.
RESTORE_NEW_TAB_PAGE = 5
# Sets <select>/<input> values by field name without dispatching any events, so
# an AutoPostBack control cannot start a page load of its own.
SET_FIELD_JS = """([name, value]) => {
  const el = document.getElementsByName(name)[0];
  if (!el) throw new Error("no form field named " + name);
  el.value = value;
  if (el.value !== value) throw new Error("value " + value + " not accepted by " + name);
}"""


class BrowserBusy(Exception):
    def __init__(self) -> None:
        super().__init__("browser busy in another session")


class NeedsLogin(Exception):
    def __init__(self, site: str, reason: str) -> None:
        super().__init__(f"{site} needs login: {reason}")
        self.site = site
        self.reason = reason


class FetchFailed(Exception):
    def __init__(self, site: str, status_or_reason: int | str) -> None:
        super().__init__(f"{site} fetch failed: {status_or_reason}")
        self.site = site
        self.status_or_reason = status_or_reason


@dataclass
class PageResult:
    url: str
    final_url: str
    html: str
    fetched_at: datetime


class ProfileLock:
    """Exclusive, non-blocking advisory lock on the profile directory.

    Release closes the descriptor without ``flock(LOCK_UN)``: the lock belongs to
    the open file description, so unlocking would also drop it for a child that
    inherited the fd (``fileno``). It is freed once no process holds the file open.
    The lock file is left in place.
    """

    def __init__(self, profile_dir: Path) -> None:
        self._path = Path(profile_dir) / LOCK_FILE
        self._fd: int | None = None

    def __enter__(self) -> Self:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise BrowserBusy() from None
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd
        return self

    def fileno(self) -> int:
        """The locked descriptor. A child given it (``pass_fds``) shares the lock,
        which then lasts until both this holder and the child are gone."""
        if self._fd is None:
            raise RuntimeError("profile lock is not held")
        return self._fd

    def __exit__(self, *exc: object) -> None:
        if self._fd is not None:
            fd, self._fd = self._fd, None
            os.close(fd)


@lru_cache(maxsize=1)
def _default_rules() -> GuardRules:
    return GuardRules.load()


def is_login_url(url: str, rules: GuardRules) -> bool:
    """A login host, or a Basketball Monster path containing ``/login``."""
    try:
        parts = urlsplit(url)
        host = normalize_host(parts.hostname)
    except ValueError:
        return False
    if host in rules.hosts.login:
        return True
    return host in rules.hosts.bbm and "/login" in parts.path.lower()


def detect_auth_problem(
    site: str, final_url: str, html: str, rules: GuardRules | None = None
) -> str | None:
    """Why this page shows we are not (usably) logged in, or None if it looks fine."""
    rules = rules or _default_rules()
    if is_login_url(final_url, rules):
        return LOGIN_REASON
    text = html.lower()
    for marker in CAPTCHA_MARKERS:
        if marker in text:
            return f"captcha page ({marker!r})"
    if site == "bbm" and BBM_LOGGED_OUT_MARKER in text:
        try:
            path = urlsplit(final_url).path.lower()
        except ValueError:
            path = ""
        if "projections" in path:
            return "logged out of Basketball Monster (membership prompt)"
    return None


def disable_session_restore(profile_dir: Path) -> None:
    """Make Chrome open a New Tab page at startup instead of restoring old tabs.

    Merges into ``Default/Preferences`` (created if missing), keeping every other key.
    """
    prefs_path = Path(profile_dir) / "Default" / "Preferences"
    prefs: dict[str, Any] = {}
    if prefs_path.exists():
        try:
            loaded = json.loads(prefs_path.read_text(encoding="utf-8"))
        except ValueError:
            loaded = {}  # Chrome would discard an unreadable file too
        if isinstance(loaded, dict):
            prefs = loaded
    session = prefs.get("session")
    if not isinstance(session, dict):
        session = {}
    session["restore_on_startup"] = RESTORE_NEW_TAB_PAGE
    session["startup_urls"] = []
    prefs["session"] = session
    prefs_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = prefs_path.with_name(prefs_path.name + ".h2h-tmp")
    tmp.write_text(json.dumps(prefs), encoding="utf-8")
    tmp.replace(prefs_path)


def _strip_fragment(url: str) -> str:
    return url.split("#", 1)[0]


def _first_line(exc: BaseException) -> str:
    return (str(exc).strip().splitlines() or [type(exc).__name__])[0]


class BrowserSession:
    def __init__(
        self,
        cache: Cache,
        limiter: RateLimiter,
        rules: GuardRules,
        headless: bool = False,
        *,
        minimized: bool = True,
        sleep: Callable[[float], None] = time.sleep,
        proxy: dict[str, str] | None = None,
    ) -> None:
        self._cache = cache
        self._limiter = limiter
        self._rules = rules
        self._headless = headless
        self._minimized = minimized
        self._sleep = sleep
        self._proxy = proxy
        self._stack: ExitStack | None = None
        self._context: Any = None
        self._page: Any = None
        self._last_blocked_navigation: str | None = None

    # -- lifecycle -------------------------------------------------------------

    def __enter__(self) -> Self:
        stack = ExitStack()
        try:
            # The lock comes first so a second instance never starts Chrome.
            stack.enter_context(ProfileLock(paths.profile_dir()))
            disable_session_restore(paths.profile_dir())
            playwright = sync_playwright().start()
            stack.callback(playwright.stop)
            options: dict[str, Any] = {
                "channel": "chrome",
                "headless": self._headless,
                "service_workers": "block",  # route() cannot see SW traffic
                "accept_downloads": True,
                "ignore_default_args": [MOCK_KEYCHAIN_ARG],
            }
            if not self._headless and self._minimized:
                options["args"] = ["--start-minimized"]
            if self._proxy is not None:
                options["proxy"] = self._proxy
            context = playwright.chromium.launch_persistent_context(
                str(paths.profile_dir()), **options
            )
            stack.callback(context.close)
            context.route("**/*", self._on_route)
            context.route_web_socket("**/*", self._on_web_socket)
            pages = list(context.pages)
            page = pages[0] if pages else context.new_page()
            for extra in pages[1:]:
                extra.close()
            # Anything the first page loaded before the hook existed is dropped.
            page.goto("about:blank")
            self._page = page
            context.on("page", self._on_new_page)
        except BaseException:
            self._page = None
            stack.close()
            raise
        self._stack, self._context, self._page = stack, context, page
        return self

    def __exit__(self, *exc: object) -> None:
        stack, self._stack = self._stack, None
        self._context = self._page = None
        if stack is not None:
            stack.close()

    @property
    def page(self) -> Any:
        return self._page

    @property
    def context(self) -> Any:
        return self._context

    # -- guard hooks -----------------------------------------------------------

    def _on_route(self, route: Any, request: Any) -> None:
        method, url = "?", "?"
        try:
            method, url = request.method, request.url
            is_navigation = request.is_navigation_request()
            info = RequestInfo(
                method=method,
                url=url,
                resource_type=request.resource_type,
                is_navigation=is_navigation,
                post_data=request.post_data,
            )
            verdict = decide(info, self._rules)
            allowed, reason = verdict.allowed, verdict.reason
            if not allowed and is_navigation and request.frame.parent_frame is None:
                self._last_blocked_navigation = url
        except Exception as exc:  # noqa: BLE001 - fail closed
            allowed = False
            reason = f"deny: route handler error ({type(exc).__name__})"
        if allowed:
            route.continue_()
            return
        with suppress(Exception):  # already handled, or the page is gone
            route.abort("blockedbyclient")
        self._log_blocked(method, url, reason)

    def _on_web_socket(self, ws: Any) -> None:
        url = "?"
        try:
            url = ws.url
            ws.close()
        finally:
            self._log_blocked("WEBSOCKET", url, "deny: websockets are never allowed")

    def _on_new_page(self, page: Any) -> None:
        if page is self._page:
            return
        url = "?"
        try:
            url = page.url
            page.close()
        finally:
            self._log_blocked("GET", url, "deny: popup closed")

    def _log_blocked(self, method: str, url: str, reason: str) -> None:
        site = site_of(url, self._rules) or "other"
        self._cache.log_request(site, method, url, BLOCKED_VERDICT, reason)

    # -- navigation ------------------------------------------------------------

    @contextmanager
    def _navigation(self, site: str) -> Iterator[None]:
        """Turn Playwright navigation errors into NeedsLogin / FetchFailed."""
        self._last_blocked_navigation = None
        try:
            yield
        except PlaywrightError as exc:
            if "ERR_BLOCKED_BY_CLIENT" in str(exc):
                blocked = self._last_blocked_navigation
                if blocked is not None and is_login_url(blocked, self._rules):
                    raise NeedsLogin(site, LOGIN_REASON) from exc
                raise FetchFailed(site, "navigation blocked by guard") from exc
            raise FetchFailed(site, f"navigation error: {_first_line(exc)}") from exc

    def _check_redirects(self, site: str, response: Any) -> None:
        hops = []
        request = response.request
        while request is not None:
            hops.append(request)
            request = request.redirected_from
        hops.reverse()  # original request first
        if any(is_login_url(hop.url, self._rules) for hop in hops):
            raise NeedsLogin(site, LOGIN_REASON)
        # The original request went through the route hook; later hops did not.
        for hop in hops[1:]:
            if hop.method.upper() not in READ_METHODS:
                raise FetchFailed(site, "unsafe redirect")
            as_get = RequestInfo("GET", hop.url, "document", True)
            if not decide(as_get, self._rules).allowed:
                raise FetchFailed(site, "unsafe redirect")

    def _load(
        self, site: str, url: str, navigate: Callable[[], Any]
    ) -> tuple[int, PageResult]:
        """One rate-limited page load: navigate, then run every safety check."""
        self._limiter.acquire(site)
        try:
            with self._navigation(site):
                response = navigate()
        finally:
            # Count the attempt even if it failed: the site may have seen it.
            self._limiter.record_load(site, url)
        if response is None:
            raise FetchFailed(site, "no response")
        self._check_redirects(site, response)
        self._page.wait_for_load_state("domcontentloaded")
        final_url = self._page.url
        if _strip_fragment(final_url) != _strip_fragment(response.url):
            # Meta refresh / script navigation: not rate-limited, not checked.
            raise FetchFailed(site, "unexpected navigation")
        html = self._page.content()
        reason = detect_auth_problem(site, final_url, html, self._rules)
        if reason is not None:
            raise NeedsLogin(site, reason)
        return response.status, PageResult(url, final_url, html, datetime.now(UTC))

    def fetch(self, site: str, url: str) -> PageResult:
        def navigate() -> Any:
            return self._page.goto(url, wait_until="domcontentloaded")

        status, result = self._load(site, url, navigate)
        if _retryable(status):
            self._sleep(RETRY_DELAY_S)
            status, result = self._load(site, url, navigate)
        if status >= 400:
            raise FetchFailed(site, status)
        return result

    def postback(
        self,
        site: str,
        url: str,
        event_target: str,
        select: dict[str, str] | None = None,
    ) -> PageResult:
        """WebForms postback; ``select`` maps form field names to values.

        Values are assigned without firing ``change``, so the only page load is
        the single ``__doPostBack`` below, which goes through the normal checks.

        The guard sees only field names, so values are constrained here: only
        approved fields may be set and only an approved event target fired. This
        runs before any page interaction.
        """
        bbm = self._rules.bbm
        if event_target not in bbm.post_event_targets:
            raise FetchFailed(
                site, f"postback event target {event_target!r} not allowed"
            )
        for name in select or {}:
            if name.casefold() not in bbm.post_settable_fields:
                raise FetchFailed(site, f"postback field {name!r} may not be set")
        if self._page.url != url:
            self.fetch(site, url)
        for name, value in (select or {}).items():
            try:
                self._page.evaluate(SET_FIELD_JS, [name, value])
            except PlaywrightError as exc:
                raise FetchFailed(site, f"postback field: {_first_line(exc)}") from exc

        def navigate() -> Any:
            with self._page.expect_navigation(wait_until="domcontentloaded") as nav:
                self._page.evaluate("t => __doPostBack(t, '')", event_target)
            return nav.value

        status, result = self._load(site, url, navigate)
        if status >= 400:
            raise FetchFailed(site, status)
        return result

    def download(self, site: str, url: str, trigger_selector: str) -> bytes:
        if trigger_selector not in self._rules.bbm.export_selectors:
            raise ValueError(
                f"selector {trigger_selector!r} is not an allowed export control"
            )
        if self._page.url != url:
            self.fetch(site, url)
        self._limiter.acquire(site)
        try:
            with self._page.expect_download() as info:
                self._page.click(trigger_selector)
            path = info.value.path()
        except PlaywrightError as exc:
            raise FetchFailed(site, f"download failed: {_first_line(exc)}") from exc
        finally:
            self._limiter.record_load(site, url)
        return Path(path).read_bytes()


def _retryable(status: int) -> bool:
    return status in RETRY_STATUSES or status >= 500
