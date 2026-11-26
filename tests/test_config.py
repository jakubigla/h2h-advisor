import pytest

from h2h_advisor import paths
from h2h_advisor.config import load_config
from h2h_advisor.identity import normalize_name


def test_defaults_when_no_file(home):
    c = load_config()
    assert c.limits.yahoo_daily_pages == 250 and c.limits.bbm_daily_pages == 80
    assert c.ttl_s["matchup"] == 1200 and c.ttl_s["settings"] == 7 * 86400
    assert c.ttl_s["bbm_day"] == 3 * 3600 and c.ttl_s["bbm_ros"] == 12 * 3600


def test_never_drop_merges_global_and_league(home):
    paths.config_file().parent.mkdir(parents=True)
    paths.config_file().write_text(
        '[global]\nnever_drop=["Victor Wembanyama"]\n'
        '[league."123"]\nnever_drop=["Tyrese Haliburton"]\nstream_slots=5\n'
    )
    c = load_config()
    assert c.never_drop_for("123") == {"victor wembanyama", "tyrese haliburton"}
    assert c.never_drop_for("999") == {"victor wembanyama"}
    assert c.leagues["123"].stream_slots == 5


def test_paths_respect_env(home):
    assert paths.cache_db().is_relative_to(home)


def test_never_drop_uses_roster_name_normalisation(home):
    paths.config_file().parent.mkdir(parents=True)
    paths.config_file().write_text(
        '[global]\nnever_drop=["Nikola Jokić", "Jaren Jackson Jr."]\n'
    )
    c = load_config()
    assert c.never_drop_for("1") == {normalize_name("Nikola Jokic"), "jaren jackson"}


def _write(text):
    paths.config_file().parent.mkdir(parents=True, exist_ok=True)
    paths.config_file().write_text(text, encoding="utf-8")


def test_build_parsed_per_league(home):
    _write(
        '[league."123"]\nstream_slots=5\n'
        'build = { strong = ["BLK", "PTS"], punt = ["FG_PCT"], weak = ["REB"] }\n'
        '[league."456"]\nstream_slots=2\n'
    )
    c = load_config()
    assert c.leagues["123"].build == {"strong": ["BLK", "PTS"], "punt": ["FG_PCT"], "weak": ["REB"]}
    assert c.leagues["456"].build is None


def test_light_day_max_games(home):
    assert load_config().light_day_max_games == 4
    _write("[global]\nlight_day_max_games=6\n")
    assert load_config().light_day_max_games == 6


@pytest.mark.parametrize(
    "text", ['[global]\nnever_drop="Nikola Jokic"\n', '[league."1"]\nnever_drop="Nikola Jokic"\n']
)
def test_never_drop_string_rejected(home, text):
    _write(text)
    with pytest.raises(ValueError, match="never_drop") as exc:
        load_config()
    assert str(paths.config_file()) in str(exc.value)


def test_bad_toml_error_names_file(home):
    _write("[global\nnever_drop = [")
    with pytest.raises(ValueError) as exc:
        load_config()
    assert str(paths.config_file()) in str(exc.value)


def test_config_read_as_utf8(home):
    _write('[global]\nnever_drop=["Nikola Jokić"]\n')
    assert load_config().never_drop == ["Nikola Jokić"]


def test_bbm_league_id_per_league(home):
    _write('[league."123"]\nbbm_league_id = "4567"\n[league."456"]\nstream_slots=2\n')
    c = load_config()
    assert c.leagues["123"].bbm_league_id == "4567"
    assert c.leagues["456"].bbm_league_id is None


@pytest.mark.parametrize(
    ("text", "key"),
    [
        ('[league."123"]\nstream_slot=5\n', "stream_slot"),
        ("[limits]\nyahoo_pages=10\n", "yahoo_pages"),
    ],
)
def test_unknown_key_is_a_value_error_naming_file_and_key(home, text, key):
    _write(text)
    with pytest.raises(ValueError, match=key) as exc:
        load_config()
    assert str(paths.config_file()) in str(exc.value)
