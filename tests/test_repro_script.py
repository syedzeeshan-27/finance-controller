"""The cross-platform repro script and the close's JSON round-trip."""

import importlib.util
import json
import os

from controller.close import daily_close
from controller.verify_close import verify_close

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_WORLD = os.path.join("data", "seeds", "42")


def _load_repro():
    path = os.path.join(_ROOT, "scripts", "repro.py")
    spec = importlib.util.spec_from_file_location("repro", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_repro_steps_enumerable():
    repro = _load_repro()
    numbers = [n for n, _, _ in repro.STEPS]
    assert numbers == list(range(1, len(repro.STEPS) + 1))
    for _, title, fn in repro.STEPS:
        assert title.strip()
        assert callable(fn)


def test_repro_step_spec_parser():
    repro = _load_repro()
    assert repro._parse_steps("2-4") == {2, 3, 4}
    assert repro._parse_steps("1,3,7") == {1, 3, 7}
    assert repro._parse_steps("1, 2-3") == {1, 2, 3}


def test_close_json_round_trips_through_verify_close(tmp_path):
    close = daily_close(_WORLD)
    path = tmp_path / "close.json"
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(close.to_dict(), f, ensure_ascii=False, indent=1)
    with open(path, encoding="utf-8") as f:
        loaded = json.load(f)
    assert verify_close(_WORLD, loaded) == []
