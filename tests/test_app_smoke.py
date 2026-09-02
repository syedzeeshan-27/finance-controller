"""Dashboard smoke tests: the app renders end-to-end on every world class.

src/app.py is a flat Streamlit script — every tab body executes on every
rerun, so one uncaught exception blanks every tab below it. The strongest
cheap invariant is therefore "a full run raises nothing", checked on both
world classes: the generated seed world (the full demo, which must not
degrade) and the bank-only real-statement world (header-only payments /
settlements / order_book, no golden_manifest.json, no gstr2b.csv — the
shape every agent.intake onboarding produces). The second test is the
exact repro of the Journey KeyError / Forecast FileNotFoundError crashes.
"""

import os

from streamlit.testing.v1 import AppTest

APP = os.path.join(os.path.dirname(__file__), os.pardir, "src", "app.py")
GUARD = "Not available for this world"


def _run_app() -> AppTest:
    at = AppTest.from_file(APP, default_timeout=120)
    at.run()
    return at


def test_seed_world_renders_all_tabs_clean():
    at = _run_app()
    assert not at.exception, at.exception[0].value
    # The full-demo world has every capability: nothing may degrade.
    assert not [i for i in at.info if GUARD in i.value]


def test_bank_only_world_degrades_gracefully():
    at = _run_app()
    at.sidebar.selectbox[0].select("Statement A (real bank data)").run()
    assert not at.exception, at.exception[0].value
    guards = [i for i in at.info if GUARD in i.value]
    # One guard per dependent section: Overview match-confidence, Matches,
    # Journey, Forecast, Tax. The exact count IS the spec — adding or
    # removing a guarded section must update this deliberately.
    assert len(guards) == 5
