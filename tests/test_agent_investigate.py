"""Investigator agent: advisory notes only, the close is untouched."""

import json
import os

import pytest

from agent.investigate import run_investigation
from agent.provider import ReplayTransport
from controller import queue_state as QS
from controller.close import daily_close

_WORLD = os.path.join("data", "seeds", "42")
_TRANSCRIPT = os.path.join("data", "agent_transcripts", "investigate",
                           "seed42_6a547f24b524.jsonl")
_ITEM = "6a547f24b524"          # S1: settlement never hit the bank


@pytest.fixture
def state_root(tmp_path, monkeypatch):
    monkeypatch.setattr(QS, "STATE_ROOT", str(tmp_path / "state"))


class ScriptedTransport:
    model = "fake-model"

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def create(self, request):
        self.requests.append(request)
        return self.responses.pop(0)


def _first_item_id():
    close = daily_close(_WORLD).to_dict()
    view = QS.overlay(close["queue"], QS.load_state(_WORLD))
    return view[0]["item_id"], close


def test_investigator_adds_note_and_changes_nothing_else(state_root):
    item_id, before = _first_item_id()
    transport = ScriptedTransport([
        {"stop_reason": "tool_use", "content": [
            {"type": "tool_use", "id": "t1", "name": "get_queue_item",
             "input": {"item_id": item_id}}]},
        {"stop_reason": "end_turn", "content": [
            {"type": "text",
             "text": "Advisory: chase the counterparty for the missing "
                     "credit; nothing else moves until it lands."}]},
    ])
    note = run_investigation(_WORLD, item_id, transport)
    assert note.startswith("Advisory:")

    after = daily_close(_WORLD).to_dict()
    assert before == after                       # the close is untouched

    view = QS.overlay(after["queue"], QS.load_state(_WORLD))
    mine = next(v for v in view if v["item_id"] == item_id)
    assert mine["last_note"] == note
    assert mine["actions"][-1]["by"] == "agent:investigator"
    assert mine["workflow"] == "open"            # a note is not a resolution

    # the tool answered with the real item
    tool_result = transport.requests[1]["messages"][-1]["content"][0]
    assert item_id in tool_result["content"]


def test_investigator_refuses_unknown_item(state_root):
    transport = ScriptedTransport([])
    with pytest.raises(SystemExit, match="no queue item"):
        run_investigation(_WORLD, "nope00000000", transport)


def _recorded_note(path):
    with open(path, encoding="utf-8") as f:
        last = [json.loads(line) for line in f][-1]
    return "".join(c["text"] for c in last["response"]["content"]
                   if c.get("type") == "text").strip()


def test_committed_investigator_recording_replays_offline(state_root):
    """No API key, no network: the committed LIVE recording replays against
    the committed world (every request hash re-checked by ReplayTransport),
    reproduces the recorded note, and leaves the close byte-identical."""
    before = daily_close(_WORLD).to_dict()
    note = run_investigation(_WORLD, _ITEM, ReplayTransport(_TRANSCRIPT))
    assert note == _recorded_note(_TRANSCRIPT)
    assert "62,630.19" in note                   # the settlement that never landed
    assert daily_close(_WORLD).to_dict() == before

    view = QS.overlay(before["queue"], QS.load_state(_WORLD))
    mine = next(v for v in view if v["item_id"] == _ITEM)
    assert mine["last_note"] == note
    assert mine["actions"][-1]["by"] == "agent:investigator"
    assert mine["workflow"] == "open"
