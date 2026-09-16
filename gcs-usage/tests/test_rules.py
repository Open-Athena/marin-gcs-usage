"""Tests for identities.yaml validation + site-JSON export (`gcs-usage rules`)."""

from pathlib import Path

import pytest

from gcs_usage.rules import check_rules, export_rules, parse_notes

YAML = """\
users:
  ryan-williams:
    aliases: [rw]
    team: infra
  russell-power:
    aliases: [rpower]  # observed as built_by
    team: infra
  larry-dial:
    team: infra  # TODO confirm team
teams: [infra, data]
prefix_owners:
  - prefix: gs://b1/datasets/
    team: data
  - prefix: gs://b-*/scratch/rw/
    user: ryan-williams
    team: infra  # personal scratch tree
"""


@pytest.fixture
def yaml_path(tmp_path: Path) -> Path:
    path = tmp_path / "identities.yaml"
    path.write_text(YAML)
    return path


def test_parse_notes_trailing_comments(yaml_path: Path):
    user_notes, prefix_notes = parse_notes(yaml_path.read_text())
    assert user_notes == {
        "russell-power": "observed as built_by",
        "larry-dial": "TODO confirm team",
    }
    assert prefix_notes == {"gs://b-*/scratch/rw/": "personal scratch tree"}


def test_export_rules_clean(yaml_path: Path):
    payload, findings = export_rules(yaml_path)
    assert findings == []
    assert payload == {
        "teams": ["infra", "data"],
        "users": [
            {"u": "ryan-williams", "team": "infra", "aliases": ["rw"]},
            {"u": "russell-power", "team": "infra", "aliases": ["rpower"], "note": "observed as built_by"},
            {"u": "larry-dial", "team": "infra", "aliases": [], "note": "TODO confirm team"},
        ],
        "prefix_owners": [
            {"prefix": "gs://b1/datasets/", "team": "data"},
            {
                "prefix": "gs://b-*/scratch/rw/",
                "team": "infra",
                "user": "ryan-williams",
                "note": "personal scratch tree",
            },
        ],
    }


def test_check_rules_findings():
    doc = {
        "users": {
            "ryan-williams": {"aliases": ["rw", "russell-power", "ryan-williams"], "team": "infra"},
            "russell-power": {"aliases": ["rw"], "team": "nosuch"},
        },
        "teams": ["infra"],
        "prefix_owners": [
            {"prefix": "gs://b1/x/", "team": "infra", "user": "ghost"},
            {"prefix": "gs://b1/x/", "team": "infra"},
            {"prefix": "b1/no-scheme", "team": "nosuch"},
        ],
    }
    assert sorted(check_rules(doc)) == sorted(
        [
            "alias 'russell-power' (of ryan-williams) shadows canonical user id 'russell-power'",
            "user ryan-williams: alias 'ryan-williams' is redundant (equals canonical id)",
            "user russell-power: team 'nosuch' not in teams ['infra']",
            "alias 'rw' appears under 2 users",
            "prefix_owners gs://b1/x/: user 'ghost' not in users map",
            "prefix_owners: 'gs://b1/x/' listed 2 times",
            "prefix_owners: 'b1/no-scheme' must look like gs://bucket/path/",
            "prefix_owners b1/no-scheme: team 'nosuch' not in teams ['infra']",
        ]
    )
