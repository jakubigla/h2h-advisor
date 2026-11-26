"""Short-lived guidance the user gives in conversation: keep a player, or drop one first.

``never_drop`` in config.toml is for players protected all season. A note is for
guidance that goes stale ("keep the backup big while the starter is out", "the bench
wing is useless, drop him first"), so every note has a last day and stops counting
after it. Notes live in a small JSON file under the data dir and are read on every
call, so a new note counts at once. They only shape the advice: nothing here touches
Yahoo.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Literal, cast

from h2h_advisor.identity import normalize_name

NoteKind = Literal["keep", "drop_first"]
KINDS: tuple[NoteKind, ...] = ("keep", "drop_first")
DEFAULT_DAYS = 14  # "for a while": long enough to matter, short enough to be re-checked
ALL_LEAGUES = "all"


@dataclass(frozen=True)
class Note:
    id: int
    kind: NoteKind  # keep: never offered as a drop; drop_first: offered first, whatever his value; his games score nothing
    player: str  # as the user named him; matched by normalised name
    league: str  # league id, or ALL_LEAGUES
    reason: str  # the user's reason, shown back with the note
    created: date
    until: date  # the last day the note counts

    def counts_on(self, today: date) -> bool:
        return today <= self.until

    def applies_to(self, league_id: str) -> bool:
        return self.league in (ALL_LEAGUES, league_id)

    @property
    def name_key(self) -> str:
        return normalize_name(self.player)


def load(path: Path) -> list[Note]:
    """Every stored note, expired ones included (missing file: none)."""
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return [
            Note(
                id=int(item["id"]),
                kind=cast("NoteKind", item["kind"]),
                player=item["player"],
                league=str(item["league"]),
                reason=item.get("reason", ""),
                created=date.fromisoformat(item["created"]),
                until=date.fromisoformat(item["until"]),
            )
            for item in raw["notes"]
        ]
    except (ValueError, KeyError, TypeError) as e:
        raise ValueError(f"unreadable notes file {path}: {e}") from e


def active(path: Path, today: date, league_id: str | None = None) -> list[Note]:
    """Notes that still count today, for one league (with the all-leagues ones) or all."""
    return [
        n
        for n in load(path)
        if n.counts_on(today) and (league_id is None or n.applies_to(league_id))
    ]


def add(
    path: Path,
    kind: str,
    player: str,
    league: str,
    reason: str,
    today: date,
    until: date | None = None,
) -> Note:
    """Store a note; a newer note on the same player in the same league replaces the older.

    The user changes their mind ("keep him" then "drop him"): the last word counts.
    Expired notes are pruned on every write.
    """
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}, got {kind!r}")
    if not player.strip():
        raise ValueError("a note needs a player name")
    until = until or today + timedelta(days=DEFAULT_DAYS)
    if until < today:
        raise ValueError(
            f"until {until.isoformat()} is already past (today is {today.isoformat()})"
        )
    existing = load(path)
    note = Note(
        id=max((n.id for n in existing), default=0) + 1,
        kind=cast("NoteKind", kind),
        player=player.strip(),
        league=league,
        reason=reason.strip(),
        created=today,
        until=until,
    )
    kept = [
        n
        for n in existing
        if n.counts_on(today)
        and not (n.league == league and n.name_key == note.name_key)
    ]
    _save(path, [*kept, note])
    return note


def remove(path: Path, note_id: int) -> Note | None:
    """Delete one note by id; the removed note, or None when there is no such id."""
    existing = load(path)
    gone = next((n for n in existing if n.id == note_id), None)
    if gone is not None:
        _save(path, [n for n in existing if n.id != note_id])
    return gone


def _save(path: Path, notes: list[Note]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "notes": [
            asdict(n) | {"created": n.created.isoformat(), "until": n.until.isoformat()}
            for n in notes
        ]
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    tmp.replace(path)  # atomic: a crash never leaves half a file
