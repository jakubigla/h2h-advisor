"""Player identity matching across Yahoo, Basketball Monster and the NBA schedule."""

import unicodedata
from collections import Counter
from collections.abc import Iterable
from typing import TypeVar

T = TypeVar("T")

# Characters NFKD does not decompose into base letter + combining mark.
_UNDECOMPOSED = str.maketrans({"ø": "o", "đ": "d", "ł": "l", "æ": "ae", "ß": "ss"})
_SUFFIXES = frozenset({"jr", "sr", "ii", "iii", "iv"})

TEAM_ALIASES: dict[str, str] = {
    "GS": "GSW",
    "NY": "NYK",
    "SA": "SAS",
    "NO": "NOP",
    "NOR": "NOP",  # Basketball Monster
    "PHO": "PHX",
    "UTAH": "UTA",
    "WSH": "WAS",
    "BKN": "BKN",
    "BRK": "BKN",
    "CHA": "CHA",
    "CHO": "CHA",
}


def normalize_name(name: str) -> str:
    """Canonical form of a player name, equal across sites for the same player."""
    decomposed = unicodedata.normalize("NFKD", name.lower())
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    mapped = stripped.translate(_UNDECOMPOSED)
    cleaned = (
        mapped.replace("'", "").replace("’", "").replace(".", "").replace("-", " ")
    )
    tokens = cleaned.split()
    if len(tokens) > 1 and tokens[-1] in _SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


def team_code(code: str) -> str:
    """NBA tricode for a site's team code; unknown codes pass through uppercased."""
    upper = code.strip().upper()
    return TEAM_ALIASES.get(upper, upper)


def player_key(name: str, team: str) -> str:
    return f"{normalize_name(name)}|{team_code(team)}"


def _first_names_align(a: str, b: str) -> bool:
    """One first token is a prefix of the other (an initial is a 1-letter token)."""
    return a.startswith(b) or b.startswith(a)


def reconcile_keys(
    yahoo_keys: Iterable[str], bbm_keys: Iterable[str]
) -> dict[str, str]:
    """Map Yahoo player keys to Basketball Monster keys for the same player.

    Yahoo shortens first names ("N. Alexander-Walker", "Nic Claxton") where BBM
    spells them out. A Yahoo key with an exact BBM match maps to itself. Otherwise
    it maps to the one BBM key with the same team, identical name tokens after the
    first, and a first token that is a prefix of the other's. Ambiguous or missing
    candidates leave the key unmapped, as do single-token names. A BBM key claimed
    by two Yahoo keys, or already exactly matched by another, is not used.
    """
    bbm = set(bbm_keys)
    mapping: dict[str, str] = {}
    pending: list[str] = []
    for key in dict.fromkeys(yahoo_keys):
        if key in bbm:
            mapping[key] = key
        else:
            pending.append(key)
    exact = set(mapping.values())
    by_team: dict[str, list[tuple[str, list[str]]]] = {}
    for key in bbm:
        name, _, team = key.rpartition("|")
        tokens = name.split()
        if len(tokens) > 1:
            by_team.setdefault(team, []).append((key, tokens))
    proposed: dict[str, str] = {}
    for key in pending:
        name, _, team = key.rpartition("|")
        tokens = name.split()
        if len(tokens) < 2:
            continue
        found = [
            k
            for k, other in by_team.get(team, [])
            if other[1:] == tokens[1:]
            and _first_names_align(tokens[0], other[0])
            and k not in exact
        ]
        if len(found) == 1:
            proposed[key] = found[0]
    claims = Counter(proposed.values())
    mapping.update({y: b for y, b in proposed.items() if claims[b] == 1})
    return mapping


class PlayerIndex:
    """Resolves a (name, team) pair against candidates keyed by ``player_key``."""

    def __init__(self, aliases: dict[str, str]) -> None:
        # Aliases are given as raw names; compare on normalised forms.
        self._aliases = {
            normalize_name(alt): normalize_name(canon) for alt, canon in aliases.items()
        }

    def _canonical_name(self, name: str) -> str:
        normalized = normalize_name(name)
        return self._aliases.get(normalized, normalized)

    def key(self, name: str, team: str) -> str:
        return f"{self._canonical_name(name)}|{team_code(team)}"

    def match(self, name: str, team: str, candidates: dict[str, T]) -> T | None:
        """Exact (aliased) key first, else the sole candidate with the same name.

        The name-only fallback covers mid-week trades; zero or several
        same-name candidates yield ``None`` rather than a guess.
        """
        key = self.key(name, team)
        if key in candidates:
            return candidates[key]
        canonical = self._canonical_name(name)
        same_name = [
            v for k, v in candidates.items() if k.rpartition("|")[0] == canonical
        ]
        return same_name[0] if len(same_name) == 1 else None
