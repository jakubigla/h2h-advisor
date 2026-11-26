"""Read-only request guard.

``decide`` is a pure function that says whether one browser request may leave
the machine. It is the safety core of the project: nothing state-changing may
reach Yahoo or Basketball Monster, so every rule here denies when in doubt and
a request is allowed only when a rule affirmatively allows it.
"""

import json
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal
from urllib.parse import parse_qs, unquote, urlsplit

READ_METHODS = frozenset({"GET", "HEAD"})
DEFAULT_PORTS = {"http": 80, "https": 443}
MAX_DECODE_ROUNDS = 10
MUTATION_WORD = re.compile(r"\bmutation\b", re.IGNORECASE)
# WebForms plumbing fields that carry no event of their own (casefolded).
BBM_ALWAYS_ALLOWED_FIELDS = frozenset(
    {
        "__eventtarget",
        "__eventargument",
        "__viewstate",
        "__viewstategenerator",
        "__eventvalidation",
        "__lastfocus",
        "__scrollpositionx",
        "__scrollpositiony",
        "__viewstateencrypted",
    }
)
# Every query key starting with this (casefolded) is denied on a GET: WebForms
# plumbing (__EVENTTARGET, __VIEWSTATE*, __CALLBACKID, __ASYNCPOST, ...) can turn
# a GET into a postback or callback, and we never send any of it on a GET.
BBM_QUERY_DENIED_PREFIX = "__"
# Path fragments denied on BBM whatever the rules file says: path-info after an
# ASP.NET page or handler ("/x.aspx/y.js" runs x.aspx, so a static suffix would
# not make it an asset), and generic/web-service handlers, which can act on a GET.
BBM_DENIED_PATH_PARTS = (".aspx/", ".axd/", ".ashx", ".asmx")


@dataclass(frozen=True)
class RequestInfo:
    method: str
    url: str
    resource_type: str
    is_navigation: bool
    post_data: str | None = None


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    reason: str


@dataclass(frozen=True)
class HostRules:
    yahoo: frozenset[str]
    bbm: frozenset[str]
    # Login pages: never fetched, only recognised (a redirect there means NeedsLogin).
    login: frozenset[str]


@dataclass(frozen=True)
class PostAllow:
    url_prefix: str
    operation: str


@dataclass(frozen=True)
class YahooRules:
    action_patterns: tuple[str, ...]
    post_allow: tuple[PostAllow, ...]


@dataclass(frozen=True)
class BbmRules:
    deny_path_patterns: tuple[str, ...]
    get_pages: frozenset[str]  # lowercased exact paths a GET/HEAD may fetch
    get_static_suffixes: tuple[str, ...]  # lowercased; page assets a GET may fetch
    post_pages: frozenset[str]
    post_event_targets: frozenset[str]
    post_submit_buttons: frozenset[str]  # casefolded field names
    post_fields: Mapping[str, frozenset[str]]  # lowercased path -> casefolded names
    # lowercased path -> patterns for numbered field families (fullmatch, casefolded names)
    post_field_patterns: Mapping[str, tuple[re.Pattern[str], ...]]
    # Fields the driver may set to a chosen value (casefolded). Every other field is
    # posted exactly as the browser rendered it; the guard only sees names, so the
    # session enforces this list before any postback.
    post_settable_fields: frozenset[str]
    # A field name or __EVENTTARGET value fullmatching one of these is always denied.
    deny_field_patterns: tuple[re.Pattern[str], ...]
    export_selectors: tuple[str, ...]


@dataclass(frozen=True)
class GuardRules:
    hosts: HostRules
    yahoo: YahooRules
    bbm: BbmRules
    blocked_resources: frozenset[str]

    @classmethod
    def load(cls, path: Path | None = None) -> "GuardRules":
        """Load rules from ``path`` or the packaged ``guard_rules.toml``.

        Every key is required: a rules file with a missing section raises
        rather than silently falling back to something more permissive.
        """
        if path is None:
            text = (
                resources.files("h2h_advisor.browser")
                .joinpath("guard_rules.toml")
                .read_text(encoding="utf-8")
            )
        else:
            text = path.read_text(encoding="utf-8")
        return cls.from_dict(tomllib.loads(text))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GuardRules":
        hosts, yahoo, bbm = data["hosts"], data["yahoo"], data["bbm"]
        bbm_rules = _build_bbm_rules(bbm)
        return cls(
            hosts=HostRules(
                yahoo=_lower_set(hosts["yahoo"]),
                bbm=_lower_set(hosts["bbm"]),
                login=_lower_set(hosts["login"]),
            ),
            yahoo=YahooRules(
                action_patterns=tuple(p.lower() for p in yahoo["action_patterns"]),
                post_allow=tuple(
                    PostAllow(url_prefix=e["url_prefix"], operation=e["operation"])
                    for e in yahoo["post_allow"]
                ),
            ),
            bbm=bbm_rules,
            blocked_resources=_lower_set(data["resources"]["block"]),
        )


def _build_bbm_rules(bbm: dict[str, Any]) -> BbmRules:
    post_pages = _lower_set(bbm["post_pages"])
    post_fields = {
        path.lower(): _fold_set(names) for path, names in bbm["post_fields"].items()
    }
    post_field_patterns = {
        path.lower(): tuple(_compile_pattern(p) for p in patterns)
        for path, patterns in bbm["post_field_patterns"].items()
    }
    deny_patterns = tuple(_compile_pattern(p) for p in bbm["deny_field_patterns"])
    rules = BbmRules(
        deny_path_patterns=tuple(p.lower() for p in bbm["deny_path_patterns"]),
        get_pages=_lower_set(bbm["get_pages"]),
        get_static_suffixes=tuple(x.lower() for x in bbm["get_static_suffixes"]),
        post_pages=post_pages,
        post_event_targets=frozenset(bbm["post_event_targets"]),
        post_submit_buttons=_fold_set(bbm["post_submit_buttons"]),
        post_fields=MappingProxyType(post_fields),
        post_field_patterns=MappingProxyType(post_field_patterns),
        post_settable_fields=_fold_set(bbm["post_settable_fields"]),
        deny_field_patterns=deny_patterns,
        export_selectors=tuple(bbm["export_selectors"]),
    )
    _check_bbm_consistency(rules)
    return rules


def _compile_pattern(pattern: str) -> re.Pattern[str]:
    """Compile a field-name pattern so it cannot be dodged.

    DOTALL: ``.*`` must also span a newline smuggled into a name. ASCII: ``\\d``
    must not accept other scripts' digits. IGNORECASE: a pattern written with a
    capital letter must not silently never match a casefolded name.
    """
    try:
        return re.compile(pattern, re.ASCII | re.DOTALL | re.IGNORECASE)
    except re.error as exc:
        raise ValueError(f"invalid regex {pattern!r} in bbm rules: {exc}") from exc


def _matches_any(patterns: tuple[re.Pattern[str], ...], name: str) -> bool:
    return any(p.fullmatch(name) for p in patterns)


def _check_bbm_consistency(rules: BbmRules) -> None:
    """Refuse rules whose allowlists contradict each other (fail at load, not at run)."""
    pages = rules.post_pages
    for path in pages:
        if path not in rules.post_fields:
            raise ValueError(f"bbm post_pages entry {path!r} has no post_fields entry")
        if path not in rules.get_pages:
            raise ValueError(f"bbm post_pages entry {path!r} is not in get_pages")
    for table, keys in (
        ("post_fields", rules.post_fields),
        ("post_field_patterns", rules.post_field_patterns),
    ):
        for path in keys:
            if path not in pages:
                raise ValueError(f"bbm {table} key {path!r} is not in post_pages")

    exact_fields = frozenset().union(*rules.post_fields.values())
    for table, names in (
        ("post_submit_buttons", rules.post_submit_buttons),
        (
            "post_event_targets",
            frozenset(t.casefold() for t in rules.post_event_targets),
        ),
    ):
        for name in names - exact_fields:
            raise ValueError(
                f"bbm {table} entry {name!r} is not an exact post_fields name"
            )

    for name in rules.post_settable_fields - exact_fields:
        raise ValueError(
            f"bbm post_settable_fields entry {name!r} is not an exact post_fields name"
        )

    allowlisted = exact_fields | rules.post_submit_buttons | rules.post_settable_fields
    allowlisted |= frozenset(t.casefold() for t in rules.post_event_targets)
    for name in sorted(allowlisted):
        if _matches_any(rules.deny_field_patterns, name):
            raise ValueError(f"bbm allowlisted name {name!r} matches a deny pattern")


def _lower_set(values: list[str]) -> frozenset[str]:
    return frozenset(v.lower() for v in values)


def _fold_set(values: list[str]) -> frozenset[str]:
    return frozenset(v.casefold() for v in values)


def _decode_fully(text: str) -> str | None:
    """Percent-decode until stable so %2561 cannot hide a pattern; None if it never settles."""
    for _ in range(MAX_DECODE_ROUNDS):
        decoded = unquote(text)
        if decoded == text:
            return text
        text = decoded
    return None


@lru_cache(maxsize=1)
def _packaged_rules() -> GuardRules:
    return GuardRules.load()


def site_of(
    url: str, rules: GuardRules | None = None
) -> Literal["yahoo", "bbm"] | None:
    """Which site a URL belongs to. Login hosts and unknown hosts give None."""
    rules = rules or _packaged_rules()
    host = _http_host(url)
    if host is None:
        return None
    if host in rules.hosts.yahoo:
        return "yahoo"
    if host in rules.hosts.bbm:
        return "bbm"
    return None


def normalize_host(host: str | None) -> str:
    """Lowercase ``host`` and drop one trailing dot (``example.com.`` is ``example.com``)."""
    host = (host or "").lower()
    return host.removesuffix(".")


def _http_host(url: str) -> str | None:
    try:
        parts = urlsplit(url)
        if parts.scheme.lower() not in DEFAULT_PORTS:
            return None
        return normalize_host(parts.hostname) or None
    except ValueError:
        return None


def decide(req: RequestInfo, rules: GuardRules) -> Verdict:
    try:
        return _decide(req, rules)
    except Exception as exc:  # noqa: BLE001 - fail closed: a guard bug must never allow
        return Verdict(False, f"deny: guard error ({type(exc).__name__})")


def _deny(reason: str) -> Verdict:
    return Verdict(False, f"deny: {reason}")


def _allow(reason: str) -> Verdict:
    return Verdict(True, f"allow: {reason}")


def _decide(req: RequestInfo, rules: GuardRules) -> Verdict:
    method = req.method.strip().upper()

    try:
        parts = urlsplit(req.url)
        scheme = parts.scheme.lower()
        host = normalize_host(parts.hostname)
        port = parts.port
    except ValueError:
        return _deny("malformed URL")

    if scheme == "about":
        if req.url == "about:blank" and req.is_navigation and method == "GET":
            return _allow("about:blank navigation")
        return _deny("about: URL other than a about:blank navigation")
    if scheme not in DEFAULT_PORTS:
        return _deny(f"scheme '{scheme}' is not http(s)")
    if not host:
        return _deny("URL has no host")
    if parts.username is not None or parts.password is not None:
        return _deny("URL contains credentials")
    if port is not None and port != DEFAULT_PORTS[scheme]:
        return _deny(f"non-default port {port}")

    is_yahoo = host in rules.hosts.yahoo
    is_bbm = host in rules.hosts.bbm
    is_login = host in rules.hosts.login

    # Match against the percent-decoded form so %61ddplayer cannot slip through.
    decoded_path = _decode_fully(parts.path)
    decoded_query = _decode_fully(parts.query)
    if decoded_path is None or decoded_query is None:
        return _deny("URL percent-encoding does not decode to a stable form")
    path = decoded_path.lower()
    query = decoded_query.lower()

    if not (is_yahoo or is_bbm or is_login):
        return _deny(f"foreign host {host}")

    if req.resource_type.lower() in rules.blocked_resources:
        return _deny(f"blocked resource type {req.resource_type.lower()}")

    if is_login:
        return _deny("login host (log in with `h2h-advisor login`)")

    if is_yahoo:
        return _decide_yahoo(req, method, path, query, rules)
    return _decide_bbm(req, method, path, query, rules)


def _decide_yahoo(
    req: RequestInfo, method: str, path: str, query: str, rules: GuardRules
) -> Verdict:
    target = f"{path}?{query}" if query else path
    for pattern in rules.yahoo.action_patterns:
        if pattern in target:
            return _deny(f"yahoo action pattern '{pattern}'")
    if MUTATION_WORD.search(query):
        return _deny("yahoo URL query mentions a mutation")

    if method in READ_METHODS:
        return _allow(f"yahoo {method} read")
    if method != "POST":
        return _deny(f"yahoo method {method or '<empty>'} not allowed")

    operation = _yahoo_operation(req.post_data)
    if operation is None:
        return _deny("yahoo POST body is not JSON with an operationName")
    for entry in rules.yahoo.post_allow:
        if req.url.startswith(entry.url_prefix) and operation == entry.operation:
            return _allow(f"yahoo POST allowlisted operation {operation}")
    return _deny(f"yahoo POST operation '{operation}' not in post_allow")


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _has_mutation_query(node: Any) -> bool:
    if isinstance(node, dict):
        for key, value in node.items():
            if (
                key.casefold() == "query"
                and isinstance(value, str)
                and MUTATION_WORD.search(value)
            ):
                return True
            if _has_mutation_query(value):
                return True
    elif isinstance(node, list):
        return any(_has_mutation_query(item) for item in node)
    return False


def _yahoo_operation(post_data: str | None) -> str | None:
    if not post_data:
        return None
    try:
        body = json.loads(post_data, object_pairs_hook=_no_duplicate_keys)
    except ValueError:
        return None
    if not isinstance(body, dict) or _has_mutation_query(body):
        return None
    operation = body.get("operationName")
    return operation if isinstance(operation, str) and operation else None


def _decide_bbm(
    req: RequestInfo, method: str, path: str, query: str, rules: GuardRules
) -> Verdict:
    matched = [p for p in rules.bbm.deny_path_patterns if p in path]
    if matched:
        return _deny(f"bbm deny path pattern '{matched[0]}'")
    handler = [p for p in BBM_DENIED_PATH_PARTS if p in path]
    if handler:
        return _deny(f"bbm path-info or handler '{handler[0]}' in path")

    if method in READ_METHODS:
        return _decide_bbm_read(path, query, rules)
    if method != "POST":
        return _deny(f"bbm method {method or '<empty>'} not allowed")

    if path not in rules.bbm.post_pages:
        return _deny(f"bbm POST to {path} not in post_pages")
    if query:
        # The form action carries no query; one here is an attempt to smuggle fields.
        return _deny("bbm POST URL has a query string")
    return _decide_bbm_post_body(req.post_data, path, rules)


def _decide_bbm_read(path: str, query: str, rules: GuardRules) -> Verdict:
    """GET/HEAD: allowlisted pages and assets only, and never a GET-borne postback.

    WebForms runs a postback from the query string too, so a query naming a
    WebForms event field or any control we would not post is denied.
    """
    page = path or "/"
    if page not in rules.bbm.get_pages and not page.endswith(
        rules.bbm.get_static_suffixes
    ):
        return _deny(f"bbm GET {page} not in get_pages or a static asset")
    for key in parse_qs(query, keep_blank_values=True):
        if _is_denied_query_key(key.casefold(), rules.bbm):
            return _deny(f"bbm GET query field '{key}' is a form or event field")
    return _allow("bbm GET read")


def _is_denied_query_key(key: str, bbm: BbmRules) -> bool:
    return (
        key.startswith(BBM_QUERY_DENIED_PREFIX)
        or _matches_any(bbm.deny_field_patterns, key)
        or key in bbm.post_submit_buttons
        or key in {t.casefold() for t in bbm.post_event_targets}
    )


def _decide_bbm_post_body(
    post_data: str | None, path: str, rules: GuardRules
) -> Verdict:
    page_fields = rules.bbm.post_fields.get(path)
    if page_fields is None:
        return _deny(f"bbm POST to {path} has no post_fields entry")
    if not post_data:
        return _deny("bbm POST has no form body")
    try:
        raw_fields = parse_qs(post_data, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        return _deny("bbm POST body is not a url-encoded form")

    # ASP.NET reads Request.Form case-insensitively, so fold names before any check.
    fields: dict[str, list[str]] = {}
    for name, values in raw_fields.items():
        fields.setdefault(name.casefold(), []).extend(values)

    # Deny patterns are checked first and override every allow below.
    deny = rules.bbm.deny_field_patterns
    for name in fields:
        if _matches_any(deny, name):
            return _deny(f"bbm POST field '{name}' matches a deny pattern")
    for target in fields.get("__eventtarget", []):
        if _matches_any(deny, target.casefold()):
            return _deny(f"bbm POST __EVENTTARGET '{target}' matches a deny pattern")

    # Our driver always posts an empty __EVENTARGUMENT; anything else is unreviewed.
    if any(fields.get("__eventargument", [])):
        return _deny("bbm POST has a non-empty __EVENTARGUMENT")

    page_patterns = rules.bbm.post_field_patterns.get(path, ())
    for name in fields:
        if (
            name not in BBM_ALWAYS_ALLOWED_FIELDS
            and name not in page_fields
            and not _matches_any(page_patterns, name)
        ):
            return _deny(f"bbm POST field '{name}' not in post_fields for {path}")

    # One entry per posted value, so the same button twice counts as two.
    buttons = [
        name
        for name, values in fields.items()
        if name in rules.bbm.post_submit_buttons
        for _ in values
    ]
    targets = fields.get("__eventtarget", [])
    if len(targets) > 1:
        return _deny("bbm POST has multiple __EVENTTARGET values")
    if targets and targets[0]:
        if targets[0] not in rules.bbm.post_event_targets:
            return _deny(
                f"bbm POST __EVENTTARGET '{targets[0]}' not in post_event_targets"
            )
        if buttons:
            return _deny("bbm POST has both __EVENTTARGET and a submit button field")
        return _allow(f"bbm POST allowlisted __EVENTTARGET {targets[0]}")

    if len(buttons) == 1:
        return _allow(f"bbm POST allowlisted submit button {buttons[0]}")
    return _deny("bbm POST has no allowlisted __EVENTTARGET or single submit button")
