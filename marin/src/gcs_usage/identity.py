"""Canonical identity map for storage attribution.

Loads ``identities.yaml`` and resolves raw username spellings (Iris job
owners, provenance ``built_by``, ``users/<seg>/`` path segments) to canonical
user ids and teams. The canonical spelling is
:func:`gcs_usage.usernames.sanitize_username` output, so a raw spelling that
sanitizes directly to a canonical id needs no alias entry; aliases cover
everything else.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from .usernames import sanitize_username

DEFAULT_IDENTITIES = Path(__file__).resolve().parent / "identities.yaml"
UNKNOWN_TEAM = "unknown"


@dataclass(frozen=True)
class PrefixOwner:
    """Manual owner for a shared prefix no per-user signal covers."""

    prefix: str
    team: str
    user: str | None = None


@dataclass(frozen=True)
class IdentityMap:
    user_teams: dict[str, str]
    alias_to_user: dict[str, str]
    teams: tuple[str, ...]
    prefix_owners: tuple[PrefixOwner, ...]

    def resolve(self, raw: str) -> str:
        """Canonical user id for a raw spelling.

        An unaliased spelling resolves to its own sanitized segment: a
        ``users/<seg>/`` prefix is definitionally owned by ``<seg>``, so an
        unmapped user is still attributed (with team ``unknown``) rather than
        dropped. Unknown-team users are surfaced by reports to drive map
        curation.
        """
        segment = sanitize_username(raw)
        return self.alias_to_user.get(segment, segment)

    def team_of(self, user: str) -> str:
        return self.user_teams.get(user, UNKNOWN_TEAM)


def load_identities(path: Path = DEFAULT_IDENTITIES) -> IdentityMap:
    """Load and validate the identity map; raises ``ValueError`` on a bad map."""
    with open(path) as f:
        doc = yaml.safe_load(f)
    teams = tuple(doc.get("teams") or ())
    user_teams: dict[str, str] = {}
    alias_to_user: dict[str, str] = {}
    for user, entry in (doc.get("users") or {}).items():
        if sanitize_username(user) != user:
            raise ValueError(f"canonical user id {user!r} is not in sanitized form")
        team = entry.get("team")
        if team not in teams:
            raise ValueError(f"user {user!r}: team {team!r} not in teams {list(teams)}")
        user_teams[user] = team
    for user, entry in (doc.get("users") or {}).items():
        for alias in entry.get("aliases") or ():
            segment = sanitize_username(alias)
            if segment in user_teams and segment != user:
                raise ValueError(f"alias {alias!r} of {user!r} collides with canonical user {segment!r}")
            existing = alias_to_user.get(segment)
            if existing is not None and existing != user:
                raise ValueError(f"alias {alias!r} ({segment!r}) maps to both {existing!r} and {user!r}")
            alias_to_user[segment] = user
    prefix_owners = []
    for row in doc.get("prefix_owners") or ():
        team = row.get("team")
        if team not in teams:
            raise ValueError(f"prefix_owner {row.get('prefix')!r}: team {team!r} not in teams {list(teams)}")
        prefix_owners.append(PrefixOwner(prefix=row["prefix"], team=team, user=row.get("user")))
    return IdentityMap(
        user_teams=user_teams,
        alias_to_user=alias_to_user,
        teams=teams,
        prefix_owners=tuple(prefix_owners),
    )
