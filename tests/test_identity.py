import pytest

from h2h_advisor.identity import (
    PlayerIndex,
    normalize_name,
    player_key,
    reconcile_keys,
    team_code,
)


@pytest.mark.parametrize(
    "a,b",
    [
        ("Nikola Jokić", "Nikola Jokic"),
        ("Jaren Jackson Jr.", "Jaren Jackson"),
        ("Luka Dončić", "luka doncic"),
        ("Shai Gilgeous-Alexander", "Shai Gilgeous Alexander"),
        ("De'Aaron Fox", "DeAaron Fox"),
        ("De’Aaron Fox", "DeAaron Fox"),
        ("Alperen Şengün", "Alperen Sengun"),
        ("Nikola Vučević", "Nikola Vucevic"),
        ("Dennis Schröder", "Dennis Schroder"),
        ("Jusuf Nurkić", "Jusuf Nurkic"),
        ("Kristaps Porziņģis", "Kristaps Porzingis"),
        ("Jonas Valančiūnas", "Jonas Valanciunas"),
        ("Gary Trent Jr.", "gary trent"),
        ("Marvin Bagley III", "Marvin Bagley"),
        ("Tim Hardaway Jr", "Tim Hardaway"),
        ("  Victor   Wembanyama ", "victor wembanyama"),
    ],
)
def test_normalize_equivalents(a, b):
    assert normalize_name(a) == normalize_name(b)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Nikola Jokić", "nikola jokic"),
        ("De'Aaron Fox", "deaaron fox"),
        ("Shai Gilgeous-Alexander", "shai gilgeous alexander"),
        ("Jaren Jackson Jr.", "jaren jackson"),
        ("P.J. Washington", "pj washington"),
        ("Đorđe Đurić", "dorde duric"),
        ("Łukasz Kowalski", "lukasz kowalski"),
        ("Søren Æther", "soren aether"),
        ("Straße Müller", "strasse muller"),
    ],
)
def test_normalize_values(raw, expected):
    assert normalize_name(raw) == expected


def test_suffix_only_dropped_when_last_and_not_sole_token():
    assert normalize_name("Jr") == "jr"
    assert normalize_name("III") == "iii"
    assert normalize_name("Junior Bridgeman") == "junior bridgeman"
    assert normalize_name("Ii Something") == "ii something"
    assert normalize_name("Wendell Carter Jr.") == "wendell carter"


def test_team_aliases():
    assert player_key("X Y", "GS") == player_key("X Y", "GSW")


@pytest.mark.parametrize(
    "code,expected",
    [
        ("GS", "GSW"),
        ("NY", "NYK"),
        ("SA", "SAS"),
        ("NO", "NOP"),
        ("NOR", "NOP"),  # Basketball Monster
        ("PHO", "PHX"),
        ("UTAH", "UTA"),
        ("WSH", "WAS"),
        ("BKN", "BKN"),
        ("BRK", "BKN"),
        ("CHA", "CHA"),
        ("CHO", "CHA"),
        (" gs ", "GSW"),
        ("lal", "LAL"),
        ("ZZZ", "ZZZ"),
        ("zzz", "ZZZ"),
    ],
)
def test_team_code(code, expected):
    assert team_code(code) == expected


def test_player_key_format():
    assert player_key("Nikola Jokić", "den") == "nikola jokic|DEN"


def test_exact_key_match():
    idx = PlayerIndex({})
    assert idx.match("Nikola Jokic", "DEN", {player_key("Nikola Jokić", "DEN"): 1}) == 1


def test_name_only_fallback_when_unique():
    idx = PlayerIndex({})
    assert (
        idx.match("Dennis Schroder", "LAL", {player_key("Dennis Schröder", "BKN"): 1})
        == 1
    )


def test_name_only_fallback_ambiguous_returns_none():
    idx = PlayerIndex({})
    candidates = {
        player_key("Jaylin Williams", "OKC"): 1,
        player_key("Jaylin Williams", "SAS"): 2,
    }
    assert idx.match("Jaylin Williams", "LAL", candidates) is None


def test_no_match_returns_none():
    idx = PlayerIndex({})
    assert (
        idx.match("Nobody Here", "LAL", {player_key("Someone Else", "LAL"): 1}) is None
    )
    assert idx.match("Nobody Here", "LAL", {}) is None


def test_exact_key_preferred_over_ambiguity():
    idx = PlayerIndex({})
    candidates = {
        player_key("Jaylin Williams", "OKC"): 1,
        player_key("Jaylin Williams", "SAS"): 2,
    }
    assert idx.match("Jaylin Williams", "SAS", candidates) == 2


def test_alias_table():
    idx = PlayerIndex({"Herb Jones": "Herbert Jones"})
    assert idx.match("Herb Jones", "NOP", {player_key("Herbert Jones", "NOP"): 7}) == 7


def test_alias_names_are_normalised_on_both_sides():
    idx = PlayerIndex({"Nicolas Claxton": "Nic Claxtön Jr."})
    assert idx.key("nicolas claxton", "bkn") == "nic claxton|BKN"
    assert (
        idx.match("Nicolas Claxton", "BKN", {player_key("Nic Claxton", "BKN"): 3}) == 3
    )


def test_alias_with_trade_uses_name_only_fallback():
    idx = PlayerIndex({"Herb Jones": "Herbert Jones"})
    assert idx.match("Herb Jones", "LAL", {player_key("Herbert Jones", "NOP"): 7}) == 7


def test_alias_ambiguous_after_alias_returns_none():
    idx = PlayerIndex({"Herb Jones": "Herbert Jones"})
    candidates = {
        player_key("Herbert Jones", "NOP"): 7,
        player_key("Herbert Jones", "LAL"): 8,
    }
    assert idx.match("Herb Jones", "MIA", candidates) is None


def test_key_applies_alias_and_team_code():
    idx = PlayerIndex({"Herb Jones": "Herbert Jones"})
    assert idx.key("Herb Jones", "NO") == "herbert jones|NOP"


LIVE_PAIRS = [
    ("y konan niederhauser|LAC", "yanic konan niederhauser|LAC"),
    ("n alexander walker|ATL", "nickeil alexander walker|ATL"),
    ("alex sarr|WAS", "alexandre sarr|WAS"),
    ("nic claxton|CHI", "nicolas claxton|CHI"),
]


def test_live_pair_keys_come_from_the_real_names():
    assert player_key("Y. Konan Niederhäuser", "LAC") == LIVE_PAIRS[0][0]
    assert player_key("Yanic Konan Niederhauser", "LAC") == LIVE_PAIRS[0][1]
    assert player_key("N. Alexander-Walker", "ATL") == LIVE_PAIRS[1][0]
    assert player_key("Nickeil Alexander-Walker", "ATL") == LIVE_PAIRS[1][1]


@pytest.mark.parametrize("yahoo,bbm", LIVE_PAIRS)
def test_reconcile_keys_live_pairs(yahoo, bbm):
    assert reconcile_keys([yahoo], [bbm, "jalen brunson|NYK"]) == {yahoo: bbm}


def test_reconcile_keys_all_live_pairs_at_once():
    yahoo, bbm = zip(*LIVE_PAIRS, strict=True)
    assert reconcile_keys(yahoo, bbm) == dict(LIVE_PAIRS)


def test_reconcile_keys_exact_match_maps_to_itself():
    keys = ["nic claxton|CHI", "jalen brunson|NYK"]
    assert reconcile_keys(keys, keys) == {k: k for k in keys}


def test_reconcile_keys_two_candidates_on_one_team_stay_unmapped():
    bbm = ["john smith|BOS", "jake smith|BOS"]
    assert reconcile_keys(["j smith|BOS"], bbm) == {}


def test_reconcile_keys_same_name_on_a_different_team_stays_unmapped():
    assert reconcile_keys(["alex sarr|WAS"], ["alexandre sarr|CHI"]) == {}


def test_reconcile_keys_single_token_names_never_match():
    assert reconcile_keys(["nene|HOU"], ["nenebis|HOU"]) == {}
    assert reconcile_keys(["nene|HOU"], ["nene hilario|HOU"]) == {}
    assert reconcile_keys(["n hilario|HOU"], ["nene|HOU"]) == {}


def test_reconcile_keys_non_prefix_first_name_does_not_match():
    assert reconcile_keys(["nick claxton|CHI"], ["nicolas claxton|CHI"]) == {}


def test_reconcile_keys_later_tokens_must_be_identical():
    assert reconcile_keys(["alex sarr|WAS"], ["alexandre sarro|WAS"]) == {}
    assert reconcile_keys(["j walker smith|BOS"], ["john smith|BOS"]) == {}


def test_reconcile_keys_does_not_reuse_a_bbm_key_exactly_matched_elsewhere():
    yahoo = ["alexandre sarr|WAS", "alex sarr|WAS"]
    assert reconcile_keys(yahoo, ["alexandre sarr|WAS"]) == {
        "alexandre sarr|WAS": "alexandre sarr|WAS"
    }


def test_reconcile_keys_two_yahoo_keys_proposing_one_bbm_key_stay_unmapped():
    assert reconcile_keys(["a sarr|WAS", "al sarr|WAS"], ["alexandre sarr|WAS"]) == {}
