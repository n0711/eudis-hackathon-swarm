"""Mission file loading and validation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from field_fakes import mission_document

from eudis_swarm.field_mission import (
    FieldTiming,
    MissionFileError,
    load_mission,
    parse,
)

EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "field"


def test_the_shipped_example_loads() -> None:
    mission = load_mission(EXAMPLES / "mission_4uav.json")
    assert mission.agent_ids == (1, 2, 3, 4)
    assert mission.task_ids == (1, 2, 3, 4)
    assert mission.network.mode == "unicast"
    assert mission.agent(3).address == ("127.0.0.1", 47003)
    assert mission.objective(4).position == (40.0, 20.0)
    with pytest.raises(KeyError):
        mission.objective(99)
    with pytest.raises(MissionFileError):
        mission.agent(99)


def test_canonical_json_round_trips(tmp_path: Path) -> None:
    mission = parse(mission_document())
    path = tmp_path / "m.json"
    path.write_text(json.dumps(mission.to_json()))
    assert load_mission(path) == mission
    broadcast = parse(
        mission_document(
            network={
                "mode": "broadcast",
                "broadcast_address": "10.0.0.255",
                "port": 47000,
            }
        )
    )
    assert broadcast.network.broadcast_address == ("10.0.0.255", 47000)
    assert parse(broadcast.to_json()) == broadcast


def test_timing_defaults_apply() -> None:
    document = mission_document()
    del document["timing"]
    assert parse(document).timing == FieldTiming()


BAD = {
    "not object": [],
    "wrong format": mission_document(format="x"),
    "wrong version": mission_document(version=2),
    "bad id": mission_document(mission_id=""),
    "unknown key": {**mission_document(), "surprise": 1},
    "one agent": mission_document(agents=[{"id": 1, "start": [0, 0]}]),
    "duplicate agents": mission_document(
        agents=[{"id": 1, "start": [0, 0]}, {"id": 1, "start": [1, 1]}]
    ),
    "bool id": mission_document(
        agents=[{"id": True, "start": [0, 0]}, {"id": 2, "start": [1, 1]}]
    ),
    "bad start": mission_document(
        agents=[{"id": 1, "start": [0]}, {"id": 2, "start": [1, 1]}]
    ),
    "bad coordinate": mission_document(
        agents=[{"id": 1, "start": [0, "x"]}, {"id": 2, "start": [1, 1]}]
    ),
    "bad address": mission_document(
        agents=[
            {"id": 1, "start": [0, 0], "address": "nope"},
            {"id": 2, "start": [1, 1]},
        ]
    ),
    "address not string": mission_document(
        agents=[{"id": 1, "start": [0, 0], "address": 5}, {"id": 2, "start": [1, 1]}]
    ),
    "no tasks": mission_document(tasks=[]),
    "duplicate tasks": mission_document(
        tasks=[{"id": 1, "position": [0, 0]}, {"id": 1, "position": [1, 1]}]
    ),
    "zero speed": mission_document(speed=0),
    "timing unknown": mission_document(timing={"hurry": 1}),
    "timing negative": mission_document(timing={"lease_timeout": -1}),
    "timing zero": mission_document(timing={"heartbeat_interval": 0}),
    "timing bool": mission_document(timing={"heartbeat_interval": True}),
    "freshness above lease": mission_document(
        timing={"lease_timeout": 1.0, "freshness_timeout": 1.0}
    ),
    "rebroadcast too slow": mission_document(
        timing={"freshness_timeout": 0.5, "rebroadcast_interval": 0.5}
    ),
    "bad mode": mission_document(network={"mode": "mesh"}),
    "broadcast no address": mission_document(network={"mode": "broadcast", "port": 1}),
    "broadcast bad port": mission_document(
        network={"mode": "broadcast", "broadcast_address": "x", "port": 0}
    ),
}


@pytest.mark.parametrize("document", BAD.values(), ids=list(BAD))
def test_invalid_missions_are_refused(document) -> None:
    with pytest.raises(MissionFileError):
        parse(document)


def test_unreadable_files_are_refused(tmp_path: Path) -> None:
    with pytest.raises(MissionFileError, match="cannot read"):
        load_mission(tmp_path / "missing.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{")
    with pytest.raises(MissionFileError, match="not JSON"):
        load_mission(bad)
