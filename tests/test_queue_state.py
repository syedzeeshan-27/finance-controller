"""Queue workflow state: stable ids, overlay, reopen detection, purity."""

import json
import os
import sys

import pytest

from controller import queue_state as QS
from controller.close import daily_close

_WORLD = os.path.join("data", "seeds", "42")


@pytest.fixture
def state_root(tmp_path, monkeypatch):
    root = tmp_path / "state"
    monkeypatch.setattr(QS, "STATE_ROOT", str(root))
    return root


def _item(status="ambiguous_abstain", records=("setl_A", "BANK1"),
          source="recon_a"):
    return {"source": source, "status": status,
            "record_ids": list(records), "severity": 2,
            "money_at_risk_paise": 100, "title": "t", "detail": "",
            "suggested_action": "", "evidence": [], "candidates": [],
            "due_date": None, "days_overdue": None}


def test_item_id_stable_and_status_independent():
    a = QS.item_id(_item(status="ambiguous_abstain"))
    b = QS.item_id(_item(status="matched"))
    assert a == b                                   # status excluded, by design
    assert QS.item_id(_item(records=("BANK1", "setl_A"))) == a   # order-free
    assert QS.item_id(_item(records=("setl_B", "BANK1"))) != a
    assert QS.item_id(_item(source="recon_b")) != a


def test_world_name_disambiguates_generic_dirs():
    assert QS.world_name(os.path.join("data", "seeds", "42d180")) == "42d180"
    assert QS.world_name(os.path.join("data", "real", "statement_a",
                                      "world")) == "statement_a_world"


def test_append_and_overlay_round_trip(state_root):
    item = _item()
    iid = QS.item_id(item)
    QS.append_action(_WORLD, iid, action="assign", assignee="priya",
                     status_at_action=item["status"], at="2026-08-31T10:00:00")
    QS.append_action(_WORLD, iid, action="note", note="checked with bank",
                     status_at_action=item["status"], at="2026-08-31T10:05:00")
    view = QS.overlay([item], QS.load_state(_WORLD))[0]
    assert view["workflow"] == "assigned" and view["assignee"] == "priya"
    assert view["last_note"] == "checked with bank"
    assert view["reopened"] is False
    QS.append_action(_WORLD, iid, action="resolve",
                     status_at_action=item["status"])
    view = QS.overlay([item], QS.load_state(_WORLD))[0]
    assert view["workflow"] == "resolved"
    assert len(view["actions"]) == 3                # append-only trail


def test_overlay_marks_reopened_on_status_change(state_root):
    item = _item(status="ambiguous_abstain")
    iid = QS.item_id(item)
    QS.append_action(_WORLD, iid, action="resolve",
                     status_at_action="ambiguous_abstain")
    changed = _item(status="exception_missing_bank")
    view = QS.overlay([changed], QS.load_state(_WORLD))[0]
    assert view["reopened"] is True
    assert view["workflow"] == "reopened"


def test_state_file_is_valid_json_and_append_only(state_root):
    iid = QS.item_id(_item())
    QS.append_action(_WORLD, iid, action="note", note="first")
    QS.append_action(_WORLD, iid, action="note", note="second")
    with open(QS.state_path(_WORLD), encoding="utf-8") as f:
        raw = json.load(f)
    notes = [a["note"] for a in raw["items"][iid]["actions"]]
    assert notes == ["first", "second"]


def test_daily_close_output_unaffected_by_state_file(state_root):
    before = daily_close(_WORLD).to_dict()
    QS.append_action(_WORLD, "abc123def456", action="resolve",
                     status_at_action="whatever")
    after = daily_close(_WORLD).to_dict()
    assert before == after


def test_cli_list_then_resolve_round_trip(state_root, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv",
                        ["queue_state", _WORLD, "list"])
    QS.main()
    first_line = capsys.readouterr().out.splitlines()[0]
    iid = first_line.split()[0]
    monkeypatch.setattr(sys, "argv",
                        ["queue_state", _WORLD, "resolve", iid,
                         "--note", "done", "--by", "tester"])
    QS.main()
    assert "resolve recorded" in capsys.readouterr().out
    view = QS.build_queue_view(_WORLD)
    resolved = next(v for v in view if v["item_id"] == iid)
    assert resolved["workflow"] == "resolved"
    assert resolved["actions"][-1]["by"] == "tester"
