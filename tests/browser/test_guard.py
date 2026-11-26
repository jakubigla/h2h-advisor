import json
from pathlib import Path
from urllib.parse import quote, urlencode

import pytest

from h2h_advisor.browser.guard import (
    GuardRules,
    RequestInfo,
    decide,
    site_of,
)

RULES_TOML = """
[hosts]
yahoo = ["basketball.fantasysports.yahoo.com", "s.yimg.com"]
bbm = ["basketballmonster.com", "www.basketballmonster.com"]
login = ["login.yahoo.com", "api.login.yahoo.com"]

[yahoo]
action_patterns = ["/addplayer", "/dropplayer", "trade", "crumb=", "/claim", "/waiver",
    "editroster", "/editsettings", "/message", "/transactions/add"]
post_allow = [
    {url_prefix = "https://basketball.fantasysports.yahoo.com/api/", operation = "GetMatchup"},
]

[bbm]
deny_path_patterns = ["/account", "/settings", "/leaguesettings", "/subscribe",
    "/payment", "/login", "/logout"]
deny_field_patterns = []
get_pages = ["/", "/dailyprojections.aspx", "/x.aspx"]
get_static_suffixes = [".css", ".js"]
post_settable_fields = ["ctl00$Main$DateDDL"]
post_pages = ["/dailyprojections.aspx"]
post_event_targets = ["ctl00$Main$DateDDL", "ctl00$Main$ExportButton"]
post_submit_buttons = ["ctl00$Main$ExportButton"]
export_selectors = ["#export"]

[bbm.post_fields]
"/dailyprojections.aspx" = ["ctl00$Main$DateDDL", "ctl00$Main$ExportButton"]

[bbm.post_field_patterns]

[resources]
block = ["image", "media", "font"]
"""

YAHOO = "https://basketball.fantasysports.yahoo.com"
BBM = "https://www.basketballmonster.com"


@pytest.fixture(scope="module")
def rules(tmp_path_factory) -> GuardRules:
    path = tmp_path_factory.mktemp("rules") / "rules.toml"
    path.write_text(RULES_TOML)
    return GuardRules.load(path)


def req(
    url: str,
    method: str = "GET",
    resource_type: str = "document",
    is_navigation: bool = True,
    post_data: str | None = None,
) -> RequestInfo:
    return RequestInfo(method, url, resource_type, is_navigation, post_data)


def gql(operation: str) -> str:
    return json.dumps({"operationName": operation, "variables": {}})


# --- brief tests -----------------------------------------------------------


def test_yahoo_post_denied(rules):
    assert decide(req(f"{YAHOO}/nba/123/", "POST"), rules).allowed is False


def test_yahoo_add_drop_get_denied(rules):
    for path in ("/nba/123/addplayer?apid=1", "/nba/123/dropplayer"):
        assert decide(req(YAHOO + path), rules).allowed is False


def test_yahoo_allowlisted_read_post_allowed(rules):
    url = f"{YAHOO}/api/x"
    ok = decide(req(url, "POST", "xhr", False, gql("GetMatchup")), rules)
    bad = decide(req(url, "POST", "xhr", False, gql("AddPlayer")), rules)
    assert ok.allowed is True
    assert bad.allowed is False


def test_bbm_post_unknown_target_denied(rules):
    body = "__EVENTTARGET=ctl00%24Main%24SaveSettings&__VIEWSTATE=abc"
    v = decide(req(f"{BBM}/dailyprojections.aspx", "POST", post_data=body), rules)
    assert v.allowed is False


def test_bbm_post_allowlisted_target_allowed(rules):
    body = "__EVENTTARGET=ctl00%24Main%24DateDDL&__VIEWSTATE=abc"
    v = decide(req(f"{BBM}/dailyprojections.aspx", "POST", post_data=body), rules)
    assert v.allowed is True


def test_bbm_post_other_page_denied(rules):
    body = "__EVENTTARGET=ctl00%24Main%24DateDDL"
    v = decide(req(f"{BBM}/playerrankings.aspx", "POST", post_data=body), rules)
    assert v.allowed is False


def test_bbm_settings_pages_denied_even_get(rules):
    for path in ("/settings/leaguesettings.aspx", "/account.aspx"):
        assert decide(req(BBM + path), rules).allowed is False


def test_foreign_host_denied(rules):
    v = decide(
        req("https://www.google-analytics.com/collect", resource_type="xhr"),
        rules,
    )
    assert v.allowed is False


def test_login_hosts_denied(rules):
    assert decide(req("https://login.yahoo.com/"), rules).allowed is False
    assert decide(req("https://login.yahoo.com/", "POST"), rules).allowed is False


def test_images_blocked(rules):
    v = decide(
        req(f"{YAHOO}/logo.png", resource_type="image", is_navigation=False),
        rules,
    )
    assert v.allowed is False


# --- ruling tests ----------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "data:text/html,<form></form>",
        "blob:https://basketball.fantasysports.yahoo.com/abc",
        "chrome-extension://abc/page.html",
        "about:srcdoc",
        "file:///etc/passwd",
        "ftp://basketball.fantasysports.yahoo.com/",
        "ws://basketball.fantasysports.yahoo.com/socket",
    ],
)
def test_non_http_schemes_denied(rules, url):
    assert decide(req(url), rules).allowed is False


def test_about_blank_navigation_allowed(rules):
    assert decide(req("about:blank"), rules).allowed is True
    assert decide(req("about:blank", is_navigation=False), rules).allowed is False
    assert decide(req("about:blank", "POST"), rules).allowed is False


def test_host_match_is_case_insensitive(rules):
    assert (
        decide(
            req("https://BASKETBALL.FantasySports.Yahoo.com/nba/123/"),
            rules,
        ).allowed
        is True
    )
    assert (
        decide(
            req("https://Basketball.Fantasysports.yahoo.COM/nba/123/addplayer"),
            rules,
        ).allowed
        is False
    )
    assert (
        decide(
            req("https://WWW.BasketballMonster.com/dailyprojections.aspx"),
            rules,
        ).allowed
        is True
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://evil-basketballmonster.com/",
        "https://basketballmonster.com.evil.example/",
        "https://sub.basketball.fantasysports.yahoo.com/",
        "https://yahoo.com/",
        "https://basketball.fantasysports.yahoo.com.evil.example/",
        "https://basketballmonster.com@evil.example/",
        "https://evil.example/@www.basketballmonster.com",
    ],
)
def test_no_suffix_or_lookalike_host_matching(rules, url):
    assert decide(req(url), rules).allowed is False


def test_userinfo_and_odd_ports_denied(rules):
    assert (
        decide(req("https://user:pw@www.basketballmonster.com/"), rules).allowed
        is False
    )
    assert (
        decide(req("https://www.basketballmonster.com:8443/"), rules).allowed is False
    )
    assert (
        decide(req("https://www.basketballmonster.com:443/x.aspx"), rules).allowed
        is True
    )


def test_bbm_deny_paths_case_insensitive(rules):
    for path in ("/Account.aspx", "/SETTINGS/LeagueSettings.aspx", "/%61ccount.aspx"):
        assert decide(req(BBM + path), rules).allowed is False


def test_bbm_post_page_match_case_insensitive_but_exact(rules):
    body = "__EVENTTARGET=ctl00%24Main%24DateDDL"
    ok = decide(req(f"{BBM}/DailyProjections.ASPX", "POST", post_data=body), rules)
    longer = decide(
        req(f"{BBM}/dailyprojections.aspx/extra", "POST", post_data=body),
        rules,
    )
    assert ok.allowed is True
    assert longer.allowed is False


def test_yahoo_action_patterns_case_insensitive(rules):
    assert decide(req(f"{YAHOO}/nba/123/AddPlayer"), rules).allowed is False
    assert decide(req(f"{YAHOO}/nba/123/x?action=EDITROSTER"), rules).allowed is False
    assert decide(req(f"{YAHOO}/nba/123/%64ropplayer"), rules).allowed is False


def test_yahoo_action_pattern_denies_even_allowlisted_post(rules):
    url = f"{YAHOO}/api/addplayer"
    v = decide(req(url, "POST", "xhr", False, gql("GetMatchup")), rules)
    assert v.allowed is False


def test_method_case_insensitive(rules):
    assert decide(req(f"{YAHOO}/nba/123/", "get"), rules).allowed is True
    assert decide(req(f"{YAHOO}/nba/123/", "post"), rules).allowed is False
    ok = decide(
        req(f"{YAHOO}/api/x", "post", "xhr", False, gql("GetMatchup")),
        rules,
    )
    assert ok.allowed is True


def test_head_treated_as_read(rules):
    assert decide(req(f"{YAHOO}/nba/123/", "HEAD"), rules).allowed is True
    assert decide(req(f"{YAHOO}/nba/123/addplayer", "HEAD"), rules).allowed is False


@pytest.mark.parametrize(
    "method", ["OPTIONS", "PUT", "DELETE", "PATCH", "TRACE", "CONNECT", ""]
)
def test_other_methods_denied(rules, method):
    for url in (f"{YAHOO}/nba/123/", f"{BBM}/dailyprojections.aspx", f"{YAHOO}/api/x"):
        v = decide(req(url, method, post_data=gql("GetMatchup")), rules)
        assert v.allowed is False


def test_options_denied_on_login_host(rules):
    assert decide(req("https://login.yahoo.com/", "OPTIONS"), rules).allowed is False


@pytest.mark.parametrize(
    "post_data",
    [
        None,
        "",
        "not json",
        "[1, 2]",
        '{"query": "mutation"}',
        '{"operationName": 5}',
        '{"operationName": ["GetMatchup"]}',
    ],
)
def test_yahoo_post_bad_body_denied(rules, post_data):
    v = decide(req(f"{YAHOO}/api/x", "POST", "xhr", False, post_data), rules)
    assert v.allowed is False


def test_yahoo_post_outside_allowed_prefix_denied(rules):
    v = decide(
        req(f"{YAHOO}/other/x", "POST", "xhr", False, gql("GetMatchup")),
        rules,
    )
    assert v.allowed is False


@pytest.mark.parametrize(
    "post_data",
    [
        None,
        "",
        '--boundary\r\nContent-Disposition: form-data; name="__EVENTTARGET"\r\n\r\nctl00$Main$DateDDL\r\n--boundary--',
        "__EVENTTARGET=ctl00%24Main%24DateDDL&__EVENTTARGET=ctl00%24Main%24SaveSettings",
        "__EVENTTARGET=ctl00%24Main%24SaveSettings&ctl00%24Main%24ExportButton=Export",
        "__VIEWSTATE=abc",
        "garbage-without-equals",
    ],
)
def test_bbm_post_bad_or_unparseable_body_denied(rules, post_data):
    v = decide(
        req(f"{BBM}/dailyprojections.aspx", "POST", post_data=post_data),
        rules,
    )
    assert v.allowed is False


def test_bbm_post_submit_field_name_allowed(rules):
    body = "__EVENTTARGET=&__VIEWSTATE=abc&ctl00%24Main%24ExportButton=Export"
    v = decide(req(f"{BBM}/dailyprojections.aspx", "POST", post_data=body), rules)
    assert v.allowed is True


def test_bbm_login_page_denied(rules):
    assert decide(req(f"{BBM}/login.aspx"), rules).allowed is False
    assert decide(req(f"{BBM}/Login.aspx"), rules).allowed is False
    body = "a=b"
    v = decide(req(f"{BBM}/login.aspx", "POST", post_data=body), rules)
    assert v.allowed is False


def test_resource_block_applies_to_login_hosts_and_case(rules):
    v = decide(
        req(
            "https://login.yahoo.com/a.woff", resource_type="Font", is_navigation=False
        ),
        rules,
    )
    assert v.allowed is False


def test_every_verdict_has_reason(rules):
    cases = [
        req(f"{YAHOO}/nba/123/"),
        req(f"{YAHOO}/nba/123/", "POST"),
        req("data:text/html,x"),
        req("https://evil.example/"),
        req(f"{BBM}/account.aspx"),
    ]
    for r in cases:
        assert decide(r, rules).reason.strip()


def test_site_of():
    assert site_of("https://basketball.fantasysports.yahoo.com/nba/1/") == "yahoo"
    assert site_of("https://S.YIMG.com/x.js") == "yahoo"
    assert site_of("https://www.basketballmonster.com/x.aspx") == "bbm"
    assert site_of("https://login.yahoo.com/") is None
    assert site_of("https://www.google.com/") is None
    assert site_of("about:blank") is None
    assert site_of("not a url") is None


def test_default_rules_load_from_package():
    rules = GuardRules.load()
    assert "basketball.fantasysports.yahoo.com" in rules.hosts.yahoo
    assert "login.yahoo.com" in rules.hosts.login
    assert isinstance(rules.bbm.export_selectors, tuple)
    # Yahoo has no POST allowances; BBM allows only the approved projection postbacks.
    assert not rules.yahoo.post_allow
    assert rules.bbm.post_pages == {"/dailyprojections.aspx", "/projections.aspx"}
    assert rules.bbm.post_event_targets == {
        "PlayerFilterControl",
        "pageTitleRowLeague",
    }
    assert rules.bbm.post_submit_buttons == {"datenextbutton", "datebackbutton"}


def test_load_rejects_missing_keys(tmp_path: Path):
    path = tmp_path / "bad.toml"
    path.write_text("[hosts]\nyahoo = []\n")
    with pytest.raises((KeyError, ValueError)):
        GuardRules.load(path)


def test_internal_error_fails_closed(rules):
    broken = RequestInfo(None, f"{YAHOO}/nba/123/", "document", True)  # type: ignore[arg-type]
    v = decide(broken, rules)
    assert v.allowed is False
    assert "guard error" in v.reason


# --- review fix regressions ------------------------------------------------

BBM_PAGE = f"{BBM}/dailyprojections.aspx"


def bbm_post(body: str, page: str = BBM_PAGE) -> RequestInfo:
    return req(page, "POST", post_data=body)


@pytest.mark.parametrize(
    "body",
    [
        # 1: dropdown value is not a submit; an unlisted button is the real event
        "__EVENTTARGET=&ctl00$Main$DateDDL=2026-11-25&ctl00$Main$SaveButton=Save",
        "ctl00$Main$DateDDL=2026-11-25&ctl00$Main$SaveButton=Save",
        # dropdown alone (no event, no submit button) is not a submit either
        "__EVENTTARGET=&ctl00$Main$DateDDL=2026-11-25",
        "ctl00$Main$DateDDL=2026-11-25",
        # 2: allowlisted target + stray button-like field
        "__EVENTTARGET=ctl00$Main$DateDDL&ctl00$Main$SaveButton=Save",
        # target plus a listed submit button is two events
        "__EVENTTARGET=ctl00$Main$DateDDL&ctl00$Main$ExportButton=Export",
        # two submit buttons, neither an event target
        "ctl00$Main$ExportButton=Export&ctl00$Main$ExportButton=Export",
        # 3: ASP.NET reads form names case-insensitively
        "__eventtarget=ctl00$Main$SaveSettings&ctl00$Main$ExportButton=x",
        "__eventtarget=ctl00$Main$DateDDL&__EVENTTARGET=ctl00$Main$SaveSettings",
        "__EventTarget=ctl00$Main$SaveSettings",
        "__EVENTTARGET=ctl00$Main$DateDDL&CTL00$MAIN$SAVEBUTTON=Save",
    ],
)
def test_bbm_post_review_probes_denied(rules, body):
    assert decide(bbm_post(body), rules).allowed is False


def test_bbm_post_legit_forms_allowed(rules):
    plumbing = "__VIEWSTATE=a&__VIEWSTATEGENERATOR=b&__EVENTVALIDATION=c&__LASTFOCUS="
    cases = [
        f"__EVENTTARGET=ctl00$Main$DateDDL&__EVENTARGUMENT=&{plumbing}&ctl00$Main$DateDDL=2026-11-25",
        f"__eventtarget=ctl00$Main$DateDDL&{plumbing}",
        f"__EVENTTARGET=&{plumbing}&ctl00$Main$DateDDL=2026-11-25&ctl00$Main$ExportButton=Export",
        f"{plumbing}&CTL00$Main$EXPORTBUTTON=Export",
    ]
    for body in cases:
        assert decide(bbm_post(body), rules).allowed is True, body


def test_bbm_post_page_without_post_fields_entry_fails_to_load(tmp_path):
    with pytest.raises(ValueError, match="post_fields"):
        load_variant(
            tmp_path,
            RULES_TOML.replace('"/dailyprojections.aspx" = ', '"/other.aspx" = '),
        )


@pytest.mark.parametrize(
    "body",
    [
        '{"operationName":"AddPlayer","operationName":"GetMatchup"}',
        '{"operationName":"GetMatchup","operationName":"AddPlayer"}',
        '{"operationName":"GetMatchup","variables":{"a":1,"a":2}}',
        '{"operationName":"GetMatchup","query":"mutation GetMatchup{addPlayer(id:1){ok}}"}',
        '{"operationName":"GetMatchup","query":"MUTATION{x}"}',
        '{"operationName":"GetMatchup","extensions":{"query":"mutation { a }"}}',
        '{"operationName":"GetMatchup","batch":[{"query":"mutation{a}"}]}',
    ],
)
def test_yahoo_json_review_probes_denied(rules, body):
    v = decide(req(f"{YAHOO}/api/x", "POST", "xhr", False, body), rules)
    assert v.allowed is False


def test_yahoo_json_plain_query_still_allowed(rules):
    body = (
        '{"operationName":"GetMatchup","query":"query GetMatchup { matchup { id } }"}'
    )
    v = decide(req(f"{YAHOO}/api/x", "POST", "xhr", False, body), rules)
    assert v.allowed is True


def test_yahoo_get_with_mutation_query_denied(rules):
    for url in (
        f"{YAHOO}/nba/123/?query=mutation%20foo",
        f"{YAHOO}/nba/123/?q=MUTATION",
    ):
        assert decide(req(url), rules).allowed is False
    assert decide(req(f"{YAHOO}/nba/123/?q=mutations"), rules).allowed is True


def test_double_encoded_paths_denied(rules):
    assert decide(req(f"{YAHOO}/nba/123/%2561ddplayer"), rules).allowed is False
    assert decide(req(f"{YAHOO}/nba/123/%252564ropplayer"), rules).allowed is False
    assert decide(req(f"{BBM}/%2561ccount.aspx"), rules).allowed is False
    assert decide(req(f"{YAHOO}/nba/123/x?%2563rumb=abc"), rules).allowed is False


def test_never_stabilising_encoding_denied(rules):
    segment = "%"
    for _ in range(12):
        segment = quote(segment, safe="")
    url = f"{YAHOO}/nba/123/{segment}"
    assert decide(req(url), rules).allowed is False


@pytest.mark.parametrize(
    "path",
    [
        "/nba/123/proposetrade",
        "/nba/123/accepttrade",
        "/nba/123/rejecttrade",
        "/nba/123/ProposeTrade?x=1",
        "/nba/123/trade",
        "/nba/123/?crumb=abcdef",
        "/nba/123/x?foo=1&Crumb=abc",
    ],
)
def test_yahoo_trade_and_crumb_urls_denied(rules, path):
    assert decide(req(YAHOO + path), rules).allowed is False
    assert decide(req(YAHOO + path, "HEAD"), rules).allowed is False


def test_rules_are_frozen_tuples(rules):
    assert isinstance(rules.yahoo.post_allow, tuple)
    assert isinstance(rules.bbm.export_selectors, tuple)
    assert isinstance(rules.yahoo.action_patterns, tuple)
    with pytest.raises(TypeError):
        rules.bbm.post_fields["/x"] = frozenset()  # type: ignore[index]


GOOGLE = "https://accounts.google.com"


def test_fetch_still_denies_google_and_bbm_logout(rules):
    assert decide(req(f"{GOOGLE}/signin"), rules).allowed is False
    assert decide(req(f"{GOOGLE}/signin", "POST"), rules).allowed is False
    assert decide(req(f"{BBM}/logout.aspx"), rules).allowed is False
    assert decide(req(f"{BBM}/login.aspx", "POST"), rules).allowed is False


def test_fetch_still_denies_crumb_on_login_host(rules):
    url = "https://login.yahoo.com/account/challenge/password?crumb=abc&done=x"
    assert decide(req(url, "POST"), rules).allowed is False


# --- trailing dots, encoded paths ---------------------------------------


def test_trailing_dot_hosts_are_classified(rules):
    yahoo = "https://basketball.fantasysports.yahoo.com."
    bbm = "https://www.basketballmonster.com."
    assert decide(req(f"{yahoo}/nba/1/addplayer"), rules).allowed is False
    assert decide(req(f"{yahoo}/nba/1", "POST"), rules).allowed is False
    assert decide(req(f"{bbm}/dailyprojections.aspx", "POST"), rules).allowed is False
    assert decide(req(f"{bbm}/account.aspx"), rules).allowed is False
    assert (
        decide(req("https://basketballmonster.com./logout.aspx"), rules).allowed
        is False
    )
    assert decide(req(f"{bbm}/dailyprojections.aspx"), rules).allowed is True
    assert decide(req(f"{yahoo}/nba/1"), rules).allowed is True


def test_trailing_dot_login_host_and_site_of(rules):
    url = "https://login.yahoo.com./x"
    assert decide(req(url), rules).allowed is False
    assert decide(req(url, "POST"), rules).allowed is False
    assert site_of("https://www.basketballmonster.com./a", rules) == "bbm"
    assert site_of("https://basketball.fantasysports.yahoo.com./a", rules) == "yahoo"


def test_bbm_logout_denied_on_www_and_encoded(rules):
    www = "https://www.basketballmonster.com"
    assert decide(req(f"{www}/logout.aspx"), rules).allowed is False
    # Matching is decode-then-match, as the server decodes too.
    assert decide(req(f"{www}/%6Cogout.aspx"), rules).allowed is False
    assert decide(req(f"{www}/%6Cogin.aspx", "POST"), rules).allowed is False


# --- Task 12a: BBM field patterns, deny patterns, approved postbacks -------

DAILY = f"{BBM}/dailyprojections.aspx"
PROJ = f"{BBM}/Projections.aspx"  # mixed case on purpose


@pytest.fixture(scope="module")
def real() -> GuardRules:
    return GuardRules.load()


def form(*pairs: tuple[str, str]) -> str:
    return urlencode(pairs)


def plumbing() -> list[tuple[str, str]]:
    return [("__VIEWSTATE", "a"), ("__VIEWSTATEGENERATOR", "b")]


def post(rules: GuardRules, url: str, *pairs: tuple[str, str]):
    return decide(req(url, "POST", post_data=form(*pairs)), rules)


def test_real_daily_filter_postback_with_pattern_fields_allowed(real):
    v = post(
        real,
        DAILY,
        ("__EVENTTARGET", "PlayerFilterControl"),
        ("GAMEDATE", "11/25/2026"),
        ("__VIEWSTATE", "a"),
        ("__VIEWSTATE3", "b"),
        ("__VIEWSTATEFIELDCOUNT", "107"),
        ("G1234", "on"),
        ("dccol_DisplayColumnsId_17", "on"),
        ("cat_2", "on"),
        ("pos_4", "on"),
        ("PuntCategoriesControl_weight_5", "1"),
        ("ctl00$ContentPlaceHolder1$UseActualResultsCheckBox", "on"),
        ("cvvalue_CustomValuesService", ""),
        ("pageTitleRowFavorites_order", ""),
    )
    assert v.allowed is True, v.reason


@pytest.mark.parametrize("button", ["DateNextButton", "DateBackButton"])
def test_real_daily_date_button_allowed(real, button):
    v = post(
        real,
        DAILY,
        ("__EVENTTARGET", ""),
        *plumbing(),
        ("GAMEDATE", "11/25/2026"),
        (button, "Next"),
    )
    assert v.allowed is True, v.reason


def test_real_projections_league_postback_mixed_case_path_allowed(real):
    v = post(
        real,
        PROJ,
        ("__EVENTTARGET", "pageTitleRowLeague"),
        ("pageTitleRowLeague", "602"),
        *plumbing(),
        ("__VIEWSTATE1", "x"),
    )
    assert v.allowed is True, v.reason


@pytest.mark.parametrize(
    "target",
    [
        "dcsave_DisplayColumnsId",
        "dcapply_DisplayColumnsId",
        "dcreset_DisplayColumnsId",
        "dccopyall_DisplayColumnsId",
        "cvadd_CustomValuesService",
        "cvremove_CustomValuesService_0",
        "cvup_CustomValuesService_1",
        "cvdown_CustomValuesService_0",
        "cvorder_CustomValuesService",
        "cvdefaults_CustomValuesService",
        "cvvalue_CustomValuesService",
        "ManagerMessagesId_dismiss_954",
        "pageTitleRowFavorites",
        "DarkToggle",
        "TeamFilterControl",
        "SHOWTABLEFILTERS",
        "DCSAVE_DisplayColumnsId",
    ],
)
@pytest.mark.parametrize("url", [DAILY, PROJ])
def test_real_unapproved_event_targets_denied(real, url, target):
    v = post(real, url, ("__EVENTTARGET", target), *plumbing())
    assert v.allowed is False


@pytest.mark.parametrize(
    "extra",
    [
        "CLEARGAMES",
        "CSVBUTTON",
        "REMAINING",
        "dcsave_DisplayColumnsId",
        "ManagerMessagesId_dismiss_954",
        "cvadd_CustomValuesService",
        "DarkToggle",
        "pageTitleRowFavorites",
        "JoshCommentsButton",
        "cvremove_CustomValuesService_0",
        "dccopyall_DisplayColumnsId",
    ],
)
def test_real_deny_field_alongside_allowed_postback_denied(real, extra):
    v = post(
        real,
        DAILY,
        ("__EVENTTARGET", "PlayerFilterControl"),
        *plumbing(),
        (extra, "x"),
    )
    assert v.allowed is False
    assert "deny pattern" in v.reason


@pytest.mark.parametrize(
    "name", ["G12x", "xG1", "__VIEWSTATEabc", "cat_", "pos_1_2", "G", "cat_1\n"]
)
def test_real_partial_pattern_matches_denied(real, name):
    v = post(
        real,
        DAILY,
        ("__EVENTTARGET", "PlayerFilterControl"),
        *plumbing(),
        (name, "x"),
    )
    assert v.allowed is False


def test_real_non_ascii_digits_do_not_match_patterns(real):
    v = post(
        real,
        DAILY,
        ("__EVENTTARGET", "PlayerFilterControl"),
        ("G\u0661\u0662", "x"),  # Arabic-Indic digits
    )
    assert v.allowed is False


def test_real_deny_pattern_not_dodged_by_newline_in_name(real):
    v = post(
        real,
        DAILY,
        ("__EVENTTARGET", "PlayerFilterControl"),
        ("cvadd_X\nY", "x"),
    )
    assert v.allowed is False
    assert "deny pattern" in v.reason


def test_real_g_pattern_not_allowed_on_projections(real):
    body = (("__EVENTTARGET", "PlayerFilterControl"), ("G1234", "on"))
    assert post(real, DAILY, *body).allowed is True
    assert post(real, PROJ, *body).allowed is False


def test_real_date_button_denied_on_projections(real):
    v = post(real, PROJ, ("__EVENTTARGET", ""), *plumbing(), ("DateNextButton", "x"))
    assert v.allowed is False


def test_real_both_buttons_denied(real):
    v = post(
        real,
        DAILY,
        ("__EVENTTARGET", ""),
        ("DateNextButton", "Next"),
        ("DateBackButton", "Back"),
    )
    assert v.allowed is False


def test_real_button_plus_event_target_denied(real):
    v = post(
        real,
        DAILY,
        ("__EVENTTARGET", "PlayerFilterControl"),
        ("DateNextButton", "Next"),
    )
    assert v.allowed is False


def test_real_event_target_value_is_case_sensitive_but_field_names_fold(real):
    # Existing convention: the __EVENTTARGET value must match exactly, field names fold.
    wrong_case_target = post(real, DAILY, ("__EVENTTARGET", "PLAYERFILTERCONTROL"))
    assert wrong_case_target.allowed is False
    folded_names = post(
        real,
        DAILY,
        ("__EventTarget", "PlayerFilterControl"),
        ("PLAYERFILTERCONTROL", "all"),
        ("gamedate", "x"),
        ("CAT_2", "on"),
    )
    assert folded_names.allowed is True, folded_names.reason


def test_real_no_event_and_no_button_denied(real):
    assert post(real, DAILY, *plumbing(), ("GAMEDATE", "x")).allowed is False


def test_real_unknown_field_denied(real):
    v = post(
        real,
        DAILY,
        ("__EVENTTARGET", "PlayerFilterControl"),
        ("ctl00$ContentPlaceHolder1$SaveButton", "Save"),
    )
    assert v.allowed is False


def test_real_other_page_still_denied(real):
    v = post(
        real, f"{BBM}/playerrankings.aspx", ("__EVENTTARGET", "PlayerFilterControl")
    )
    assert v.allowed is False


# --- load-time consistency ---------------------------------------------------


def load_variant(tmp_path: Path, text: str) -> GuardRules:
    path = tmp_path / "variant.toml"
    path.write_text(text)
    return GuardRules.load(path)


CONSISTENT_TOML = RULES_TOML.replace(
    "deny_field_patterns = []", "deny_field_patterns = ['^cvadd_.*', '^darktoggle$']"
).replace(
    "[bbm.post_field_patterns]",
    "[bbm.post_field_patterns]\n\"/dailyprojections.aspx\" = ['^g\\d+$']",
)


def test_consistent_variant_loads(tmp_path):
    loaded = load_variant(tmp_path, CONSISTENT_TOML)
    assert len(loaded.bbm.deny_field_patterns) == 2
    assert len(loaded.bbm.post_field_patterns["/dailyprojections.aspx"]) == 1


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        # submit button that is not an exact post_fields name
        (
            'post_submit_buttons = ["ctl00$Main$ExportButton"]',
            'post_submit_buttons = ["ctl00$Main$Ghost"]',
            "post_submit_buttons",
        ),
        # event target that is not an exact post_fields name
        (
            'post_event_targets = ["ctl00$Main$DateDDL", "ctl00$Main$ExportButton"]',
            'post_event_targets = ["ctl00$Main$Ghost"]',
            "post_event_targets",
        ),
        # post_pages path without post_fields entry
        (
            'post_pages = ["/dailyprojections.aspx"]',
            'post_pages = ["/dailyprojections.aspx", "/extra.aspx"]',
            "post_fields",
        ),
        # post_fields key outside post_pages
        (
            '"/dailyprojections.aspx" = ["ctl00$Main$DateDDL", "ctl00$Main$ExportButton"]',
            (
                '"/dailyprojections.aspx" = ["ctl00$Main$DateDDL", "ctl00$Main$ExportButton"]\n'
                '"/stray.aspx" = ["a"]'
            ),
            "post_fields",
        ),
        # post_field_patterns key outside post_pages
        (
            "[bbm.post_field_patterns]",
            "[bbm.post_field_patterns]\n\"/stray.aspx\" = ['^g\\d+$']",
            "post_field_patterns",
        ),
        # allowlisted target / button / exact field matching a deny pattern
        (
            "deny_field_patterns = ['^cvadd_.*', '^darktoggle$']",
            "deny_field_patterns = ['^ctl00.main.datedd.$']",
            "deny",
        ),
        (
            "deny_field_patterns = ['^cvadd_.*', '^darktoggle$']",
            "deny_field_patterns = ['^ctl00.main.exportbutton$']",
            "deny",
        ),
        # a POST page must also be a GET page
        (
            'get_pages = ["/", "/dailyprojections.aspx", "/x.aspx"]',
            'get_pages = ["/", "/x.aspx"]',
            "get_pages",
        ),
        # settable field that is not an exact post_fields name
        (
            'post_settable_fields = ["ctl00$Main$DateDDL"]',
            'post_settable_fields = ["ctl00$Main$Ghost"]',
            "post_settable_fields",
        ),
        # invalid regexes name the offender
        (
            "deny_field_patterns = ['^cvadd_.*', '^darktoggle$']",
            "deny_field_patterns = ['^(unclosed']",
            "(unclosed",
        ),
        (
            "['^g\\d+$']",
            "['^(broken']",
            "(broken",
        ),
    ],
)
def test_load_time_consistency_errors(tmp_path, old, new, message):
    assert old in CONSISTENT_TOML
    with pytest.raises(
        ValueError, match=message.replace("(", r"\(").replace("$", r"\$")
    ):
        load_variant(tmp_path, CONSISTENT_TOML.replace(old, new))


def test_load_rejects_exact_field_matching_deny_pattern(tmp_path):
    text = CONSISTENT_TOML.replace(
        '"/dailyprojections.aspx" = ["ctl00$Main$DateDDL", "ctl00$Main$ExportButton"]',
        '"/dailyprojections.aspx" = ["ctl00$Main$DateDDL", "ctl00$Main$ExportButton", "CvAdd_Foo"]',
    )
    assert "CvAdd_Foo" in text
    with pytest.raises(ValueError, match="deny"):
        load_variant(tmp_path, text)


def test_load_rejects_missing_deny_patterns_key(tmp_path):
    with pytest.raises((KeyError, ValueError)):
        load_variant(tmp_path, CONSISTENT_TOML.replace("deny_field_patterns", "other"))


def test_load_rejects_missing_post_field_patterns_table(tmp_path):
    with pytest.raises((KeyError, ValueError)):
        load_variant(
            tmp_path, CONSISTENT_TOML.replace("[bbm.post_field_patterns]", "[bbm.zzz]")
        )


# --- fix round 1: GET query, POST query, __EVENTARGUMENT, overlap ------------------


@pytest.mark.parametrize(
    "url",
    [
        f"{BBM}/dailyprojections.aspx",
        f"{BBM}/Projections.aspx",
        f"{BBM}/",
        BBM,
        f"{BBM}/styles/bbm.css?v=1",
        f"{BBM}/css/site.css?d=20260101",
        f"{BBM}/scripts/app.js",
        f"{BBM}/WebResource.axd?d=abc&t=1",
        f"{BBM}/ScriptResource.axd?d=abc&t=1",
        f"{BBM}/favicon.ico",
        f"{BBM}/images/logo.png",
        f"{BBM}/fonts/site.woff2",
        f"{BBM}/dailyprojections.aspx?GAMEDATE=11/25/2026",
    ],
)
@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_real_bbm_get_of_pages_and_assets_allowed(real, url, method):
    v = decide(req(url, method), real)
    assert v.allowed is True, v.reason


@pytest.mark.parametrize(
    "path", ["/Account.aspx", "/Settings/x.css", "/LeagueSettings.aspx"]
)
def test_real_bbm_get_deny_path_still_wins(real, path):
    v = decide(req(BBM + path), real)
    assert v.allowed is False
    assert "deny path pattern" in v.reason


@pytest.mark.parametrize(
    "path", ["/somepage.aspx", "/dailyprojections.aspx.bak", "/rankings"]
)
def test_real_bbm_get_unlisted_page_denied(real, path):
    v = decide(req(BBM + path), real)
    assert v.allowed is False
    assert "get_pages" in v.reason


@pytest.mark.parametrize(
    "query",
    [
        "__EVENTTARGET=dcsave_DisplayColumnsId&__VIEWSTATE=",
        "__viewstate1=x",
        "__VIEWSTATEGENERATOR=x",
        "%5F%5FEVENTTARGET=x",
        "%255F%255FEVENTTARGET=x",
        "__EVENTARGUMENT=x",
        "__EVENTVALIDATION=x",
        "__LASTFOCUS=x",
        "PlayerFilterControl=AllPlayers",
        "pageTitleRowLeague=602",
        "DateNextButton=Next",
        "dcsave_DisplayColumnsId=1",
        "CLEARGAMES=x",
        "cvadd_CustomValuesService=1",
        "ok=1&__eventtarget",
        "a=1%26__eventtarget%3Dx",
        "__callbackid=ctl00",
        "__CALLBACKPARAM=x",
        "%5F%5Fasyncpost=true",
        "__previouspage=x",
    ],
)
@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_real_bbm_get_with_postback_query_denied(real, query, method):
    v = decide(req(f"{BBM}/dailyprojections.aspx?{query}", method), real)
    assert v.allowed is False
    assert "query" in v.reason


def test_real_bbm_get_query_check_applies_to_static_assets(real):
    v = decide(req(f"{BBM}/styles/bbm.css?__EVENTTARGET=x"), real)
    assert v.allowed is False


@pytest.mark.parametrize(
    "query",
    [
        "CLEARGAMES=x&__EVENTTARGET=dcsave_DisplayColumnsId",
        "x=1",
        "__EVENTTARGET=PlayerFilterControl",
    ],
)
def test_real_bbm_post_with_url_query_denied(real, query):
    body = form(("__EVENTTARGET", "PlayerFilterControl"), *plumbing())
    v = decide(req(f"{DAILY}?{query}", "POST", post_data=body), real)
    assert v.allowed is False
    assert "query" in v.reason


def test_real_bbm_post_without_query_still_allowed(real):
    body = form(("__EVENTTARGET", "PlayerFilterControl"), *plumbing())
    assert decide(req(DAILY, "POST", post_data=body), real).allowed is True


@pytest.mark.parametrize("value", ["x", "1", "dcsave_DisplayColumnsId", " "])
def test_real_bbm_post_non_empty_eventargument_denied(real, value):
    v = post(
        real,
        DAILY,
        ("__EVENTTARGET", "PlayerFilterControl"),
        ("__EVENTARGUMENT", value),
        *plumbing(),
    )
    assert v.allowed is False
    assert "__EVENTARGUMENT" in v.reason


def test_real_bbm_post_empty_eventargument_allowed(real):
    v = post(
        real,
        DAILY,
        ("__EVENTTARGET", "PlayerFilterControl"),
        ("__EVENTARGUMENT", ""),
        *plumbing(),
    )
    assert v.allowed is True, v.reason


def test_real_bbm_post_any_non_empty_eventargument_among_duplicates_denied(real):
    v = post(
        real,
        DAILY,
        ("__EVENTTARGET", "PlayerFilterControl"),
        ("__EVENTARGUMENT", ""),
        ("__eventargument", "x"),
    )
    assert v.allowed is False


def test_deny_pattern_wins_over_overlapping_field_pattern(tmp_path):
    text = CONSISTENT_TOML.replace("['^g\\d+$']", "['^cv.*$']")
    assert "^cv.*$" in text
    overlapping = load_variant(tmp_path, text)
    page = f"{BBM}/dailyprojections.aspx"

    def run(field: str):
        body = form(("__EVENTTARGET", "ctl00$Main$DateDDL"), (field, "x"))
        return decide(req(page, "POST", post_data=body), overlapping)

    assert run("cvfoo_x").allowed is True  # the overlapping pattern does allow it ...
    v = run("cvadd_x")  # ... but the deny pattern wins
    assert v.allowed is False
    assert "deny pattern" in v.reason


def test_packaged_post_settable_fields():
    rules = GuardRules.load()
    assert rules.bbm.post_settable_fields == {
        "gamedate",
        "playerfiltercontrol",
        "pagetitlerowleague",
    }
    assert "/dailyprojections.aspx" in rules.bbm.get_pages
    assert "/projections.aspx" in rules.bbm.get_pages


def test_load_rejects_missing_get_and_settable_keys(tmp_path):
    for key in ("get_pages", "get_static_suffixes", "post_settable_fields"):
        with pytest.raises((KeyError, ValueError)):
            load_variant(tmp_path, CONSISTENT_TOML.replace(key, "other_" + key))


# --- final review: path-info through the static-suffix rule; Yahoo lineup paths ----


@pytest.mark.parametrize(
    "path",
    [
        "/SomeAction.aspx/x.js?id=5",
        "/dailyprojections.aspx/x.css",
        "/Handler.ashx/x.css?op=delete",
        "/Handler.ashx",
        "/Service.asmx/Remove.js",
        "/api/favorites/remove.json?player=1",
        "/manifest.json",
        "/trace.axd",
        "/WebResource.axd/x.js",
    ],
)
@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_real_bbm_get_path_info_and_handlers_denied(real, path, method):
    v = decide(req(BBM + path, method), real)
    assert v.allowed is False, v.reason


@pytest.mark.parametrize(
    "path",
    [
        "/nba/123/4/startactiveplayers?date=2026-11-25",
        "/nba/123/4/startactive",
        "/nba/123/4/autofill?date=2026-11-25",
        "/nba/123/4/lineup?date=2026-11-25",
        "/nba/123/4/editlineup",
        "/nba/123/addplayer?apid=1",
        "/nba/123/4/dropplayer?pid=1",
        "/nba/123/4/proposetrade",
        "/nba/123/claim?pid=1",
    ],
)
def test_real_yahoo_lineup_and_roster_actions_denied(real, path):
    v = decide(req(YAHOO + path), real)
    assert v.allowed is False
    assert "action pattern" in v.reason


def test_real_yahoo_every_url_the_source_builds_is_allowed(real):
    from h2h_advisor.sources.yahoo import urls

    built = [
        urls.home(),
        urls.league_settings(123),
        urls.matchup(123),
        urls.matchup(123, week=6),
        urls.team(123, 4),
        *(urls.free_agents(123, status=s) for s in sorted(urls.STATUSES)),
        urls.free_agents(123, start=25, position="PG"),
        *(urls.free_agents(123, position=p) for p in sorted(urls.POSITIONS)),
    ]
    for url in built:
        v = decide(req(url), real)
        assert v.allowed is True, (url, v.reason)
