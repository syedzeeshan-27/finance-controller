"""Test-wide guard: never let a developer's real API key reach the tests.

`src/model.py` reads APP_USE_MOCK / ANTHROPIC_API_KEY at import time (after
load_dotenv). Setting the mock flag here — conftest imports before any test
module — pins every test to offline deterministic mode regardless of what a
local `.env` contains. load_dotenv never overrides existing environ, so this
wins over the file.
"""

import os

os.environ.setdefault("APP_USE_MOCK", "1")
