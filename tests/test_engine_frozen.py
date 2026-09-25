"""The rules engine is frozen: engine.py and normalize.py may never change.

The held-out evaluation only means something if the engine it measures was
fixed before the held-out set existed. These hashes are the files as tagged
`engine-frozen` (commit 4df9fab), with line endings normalised to LF so the
check holds on every platform. A failing test here is not a nudge to update
the hash: it means the engine was edited after the freeze, which invalidates
every held-out number built on top of it.
"""

import hashlib
import os

import pytest

_SRC = os.path.join(os.path.dirname(__file__), os.pardir, "src", "recon")

FROZEN_SHA256 = {
    "engine.py": "aeff9cbb869c70e1a712a8cb91f98687bb964b93854c08297e31aac0e05405fc",
    "normalize.py": "1c26b3dd182c6524cf2ee888fa49f2089c78beca5091cbe7f8ef70c7379250d3",
}


@pytest.mark.parametrize("name", sorted(FROZEN_SHA256))
def test_engine_file_unchanged_since_freeze(name):
    with open(os.path.join(_SRC, name), "rb") as f:
        data = f.read().replace(b"\r\n", b"\n")
    assert hashlib.sha256(data).hexdigest() == FROZEN_SHA256[name], (
        f"src/recon/{name} changed after the engine-frozen tag")
