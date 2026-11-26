from datetime import date, timedelta

import pytest

from h2h_advisor import notes, paths
from h2h_advisor.notes import ALL_LEAGUES, DEFAULT_DAYS

TODAY = date(2026, 11, 26)


def test_no_file_means_no_notes(home):
    assert notes.load(paths.notes_file()) == []
    assert notes.active(paths.notes_file(), TODAY) == []


def test_add_defaults_to_two_weeks_and_round_trips(home):
    path = paths.notes_file()
    note = notes.add(
        path, "keep", "Backup Big", "57243", "starter is out: more minutes", TODAY
    )
    assert note.until == TODAY + timedelta(days=DEFAULT_DAYS)
    assert note.created == TODAY and note.id == 1
    assert notes.load(path) == [note]


def test_note_counts_through_its_last_day_then_expires(home):
    path = paths.notes_file()
    notes.add(
        path,
        "drop_first",
        "Bench Wing",
        "57243",
        "useless",
        TODAY,
        until=TODAY + timedelta(days=3),
    )
    assert [n.player for n in notes.active(path, TODAY + timedelta(days=3))] == [
        "Bench Wing"
    ]
    assert notes.active(path, TODAY + timedelta(days=4)) == []


def test_active_filters_by_league_and_all_applies_everywhere(home):
    path = paths.notes_file()
    notes.add(path, "keep", "Shot Blocker", "57243", "blocks", TODAY)
    notes.add(path, "keep", "Victor Wembanyama", ALL_LEAGUES, "franchise", TODAY)
    assert {n.player for n in notes.active(path, TODAY, "57243")} == {
        "Shot Blocker",
        "Victor Wembanyama",
    }
    assert {n.player for n in notes.active(path, TODAY, "999")} == {"Victor Wembanyama"}


def test_a_new_note_on_the_same_player_and_league_replaces_the_old_one(home):
    path = paths.notes_file()
    notes.add(path, "keep", "Bench Wing", "57243", "upside", TODAY)
    newer = notes.add(
        path, "drop_first", "bench wing", "57243", "useless after all", TODAY
    )
    assert notes.load(path) == [newer]
    other_league = notes.add(path, "keep", "Bench Wing", "51347", "fine there", TODAY)
    assert notes.load(path) == [newer, other_league]


def test_expired_notes_are_pruned_on_write_and_ids_keep_growing(home):
    path = paths.notes_file()
    old = notes.add(
        path,
        "keep",
        "A",
        "1",
        "",
        TODAY - timedelta(days=30),
        until=TODAY - timedelta(days=1),
    )
    new = notes.add(path, "keep", "B", "1", "", TODAY)
    assert notes.load(path) == [new] and new.id > old.id


def test_remove_by_id(home):
    path = paths.notes_file()
    a = notes.add(path, "keep", "A", "1", "", TODAY)
    b = notes.add(path, "keep", "B", "1", "", TODAY)
    assert notes.remove(path, a.id) == a
    assert notes.load(path) == [b]
    assert notes.remove(path, 99) is None


@pytest.mark.parametrize(
    ("kind", "until", "message"),
    [
        ("never_drop", None, "kind must be"),
        ("keep", TODAY - timedelta(days=1), "already past"),
    ],
)
def test_bad_notes_are_refused(home, kind, until, message):
    with pytest.raises(ValueError, match=message):
        notes.add(paths.notes_file(), kind, "A", "1", "", TODAY, until=until)


def test_blank_player_is_refused(home):
    with pytest.raises(ValueError, match="player"):
        notes.add(paths.notes_file(), "keep", "  ", "1", "", TODAY)


def test_corrupt_file_names_the_path(home):
    path = paths.notes_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match=str(path)):
        notes.load(path)
