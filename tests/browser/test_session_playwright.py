"""End-to-end: a real headless Chrome behind the guard never lets a POST out.

The guard only allows default ports, so the page is requested as
``http://127.0.0.1/`` (port 80) and Chrome is pointed at a local HTTP proxy on a
random port that serves it. Playwright keeps loopback traffic on the proxy, so
every request the page makes still reaches the test server, but only after the
guard has seen it. Production rules are untouched; only this test's rules list
127.0.0.1 as a Yahoo host.
"""

import dataclasses
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar

import pytest

from h2h_advisor.browser.guard import GuardRules, HostRules
from h2h_advisor.browser.ratelimit import RateLimiter
from h2h_advisor.browser.session import BrowserSession
from h2h_advisor.cache import Cache
from h2h_advisor.config import Limits

pytestmark = pytest.mark.browser

PAGE_URL = "http://127.0.0.1/"
PAGE = b"""<!doctype html><html><body>hello
<script>fetch("/x", {method: "POST", body: "a=1"}).catch(() => {});</script>
</body></html>"""
OPENER_URL = "http://127.0.0.1/opener"
OPENER = b"""<!doctype html><html><body>opener
<script>window.open("/");</script>
</body></html>"""


class Recorder(BaseHTTPRequestHandler):
    posts: ClassVar[list[str]] = []
    gets: ClassVar[list[str]] = []

    def do_GET(self):
        type(self).gets.append(self.path)
        pages = {PAGE_URL: PAGE, OPENER_URL: OPENER}
        body = pages.get(self.path, b"")
        self.send_response(200 if body else 404)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        type(self).posts.append(self.path)
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture
def server():
    Recorder.posts, Recorder.gets = [], []
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Recorder)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture
def rules():
    base = GuardRules.load()
    hosts = HostRules(
        yahoo=frozenset({"127.0.0.1"}), bbm=base.hosts.bbm, login=base.hosts.login
    )
    return dataclasses.replace(base, hosts=hosts)


def test_page_post_is_blocked_and_logged(home, server, rules):
    cache = Cache(home / "cache.db")
    limiter = RateLimiter(cache, Limits(delay_min_s=0, delay_max_s=0))
    proxy = {"server": f"http://127.0.0.1:{server.server_address[1]}"}

    with BrowserSession(cache, limiter, rules, headless=True, proxy=proxy) as session:
        result = session.fetch("yahoo", PAGE_URL)
        for _ in range(50):
            if any(b["method"] == "POST" for b in cache.recent_blocked()):
                break
            session.page.wait_for_timeout(100)

    assert "hello" in result.html
    assert Recorder.gets, "the page itself should have been served via the proxy"
    assert Recorder.posts == []
    blocked_posts = [b for b in cache.recent_blocked() if b["method"] == "POST"]
    assert blocked_posts
    assert blocked_posts[0]["url"] == "http://127.0.0.1/x"
    assert blocked_posts[0]["site"] == "yahoo"


def test_popup_is_closed(home, server, rules):
    cache = Cache(home / "cache.db")
    limiter = RateLimiter(cache, Limits(delay_min_s=0, delay_max_s=0))
    proxy = {"server": f"http://127.0.0.1:{server.server_address[1]}"}

    with BrowserSession(cache, limiter, rules, headless=True, proxy=proxy) as session:
        session.fetch("yahoo", OPENER_URL)
        for _ in range(50):
            if any(b["reason"] == "deny: popup closed" for b in cache.recent_blocked()):
                break
            session.page.wait_for_timeout(100)
        assert session.context.pages == [session.page]

    assert any(b["reason"] == "deny: popup closed" for b in cache.recent_blocked())
