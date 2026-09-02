"""Incremental close: --as-of slicing, snapshots, stable-id deltas."""

import datetime
import os

import pytest

from controller import queue_state as QS
from controller import snapshots as SNAP
from controller.close import _merchants_rollup, daily_close

_WORLD = os.path.join("data", "seeds", "42")


@pytest.fixture
def state_root(tmp_path, monkeypatch):
    monkeypatch.setattr(QS, "STATE_ROOT", str(tmp_path / "state"))


def _last_date() -> datetime.date:
    return datetime.date.fromisoformat(daily_close(_WORLD).close_date)


def test_as_of_equal_to_last_date_matches_full_close():
    full = daily_close(_WORLD).to_dict()
    sliced = daily_close(_WORLD, as_of=_last_date()).to_dict()
    assert full == sliced


def test_as_of_earlier_date_shrinks_the_world():
    last = _last_date()
    earlier = daily_close(_WORLD,
                          as_of=last - datetime.timedelta(days=7)).to_dict()
    full = daily_close(_WORLD).to_dict()
    assert earlier["cash"]["statement_rows"] < full["cash"]["statement_rows"]
    assert earlier["close_date"] < full["close_date"]


def test_snapshot_byte_deterministic(state_root):
    close = daily_close(_WORLD).to_dict()
    path_a = SNAP.write_snapshot(close, _WORLD)
    with open(path_a, "rb") as f:
        first = f.read()
    SNAP.write_snapshot(close, _WORLD)
    with open(path_a, "rb") as f:
        assert f.read() == first


def test_delta_between_consecutive_days_keys_by_stable_id(state_root):
    last = _last_date()
    prev_close = daily_close(
        _WORLD, as_of=last - datetime.timedelta(days=1)).to_dict()
    SNAP.write_snapshot(prev_close, _WORLD)
    cur_close = daily_close(_WORLD, as_of=last).to_dict()

    prev = SNAP.latest_before(_WORLD, cur_close["close_date"])
    assert prev is not None
    d = SNAP.delta(prev, cur_close, QS.load_state(_WORLD))
    assert d["prev_close_date"] == prev_close["close_date"]
    cur_ids = {QS.item_id(i) for i in cur_close["queue"]}
    assert {i["item_id"] for i in d["new"]} | {
        i["item_id"] for i in d["carried"]} == cur_ids
    assert not ({i["item_id"] for i in d["new"]}
                & {i["item_id"] for i in d["carried"]})


def test_gone_items_split_operator_vs_data(state_root):
    close = daily_close(_WORLD).to_dict()
    prev = {
        "close_date": "2000-01-01",
        "queue": ([{"item_id": QS.item_id(i), **{k: i[k] for k in (
            "severity", "status", "source", "title",
            "money_at_risk_paise")}} for i in close["queue"]]
            + [{"item_id": "feedfeedfeed", "severity": 2, "status": "x",
                "source": "recon_a", "title": "operator killed this",
                "money_at_risk_paise": 1},
               {"item_id": "deaddeaddead", "severity": 3, "status": "y",
                "source": "recon_a", "title": "data moved on",
                "money_at_risk_paise": 2}]),
    }
    QS.append_action(_WORLD, "feedfeedfeed", action="resolve",
                     status_at_action="x")
    d = SNAP.delta(prev, close, QS.load_state(_WORLD))
    by_id = {g["item_id"]: g["resolved_by"] for g in d["gone"]}
    assert by_id == {"feedfeedfeed": "resolved_by_operator",
                     "deaddeaddead": "resolved_by_data"}
    assert d["new"] == []


def test_merchants_rollup_covers_registry(capfd):
    _merchants_rollup(os.path.join("data", "merchants.json"), horizon=14)
    out = capfd.readouterr().out
    assert "Meridian Craftworks" in out
    assert "Statement A" in out
    assert "VIOLATIONS" not in out
