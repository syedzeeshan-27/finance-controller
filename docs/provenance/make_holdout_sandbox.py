"""Build the sandbox the held-out-set author works in: generator, schemas and
loader only. No engine, no matching helpers, no docs, no reports, no agent."""

import ast
import os
import shutil

REPO = "C:/rzpay/finance-controller"
SB = "C:/Users/SYEDZE~1/AppData/Local/Temp/claude/C--rzpay/e43dec86-b62e-4eea-b8ee-4acab1eaed47/scratchpad/holdout_sandbox"

KEEP_FROM_NORMALIZE = [
    "GST_RATE_PCT",
    "paise_from_rupee_str", "paise_from_optional_rupee_str",
    "rupee_str_from_paise", "gst_on_fee",
    "parse_bank_date", "bank_date_str", "parse_iso_ts", "parse_iso_date",
    "roll_off_sunday",
]

if os.path.exists(SB):
    shutil.rmtree(SB)
os.makedirs(f"{SB}/src/recon")
os.makedirs(f"{SB}/tests")

for name in ("__init__.py", "schemas.py", "generate.py", "io_load.py"):
    shutil.copyfile(f"{REPO}/src/recon/{name}", f"{SB}/src/recon/{name}")
for name in ("conftest.py", "test_generate.py"):
    shutil.copyfile(f"{REPO}/tests/{name}", f"{SB}/tests/{name}")

src = open(f"{REPO}/src/recon/normalize.py", encoding="utf-8").read()
tree = ast.parse(src)
lines = src.splitlines(keepends=True)
chunks = []
for node in tree.body:
    names = []
    if isinstance(node, (ast.FunctionDef,)):
        names = [node.name]
    elif isinstance(node, ast.Assign):
        names = [t.id for t in node.targets if isinstance(t, ast.Name)]
    if any(n in KEEP_FROM_NORMALIZE for n in names):
        start = node.lineno - 1
        if getattr(node, "decorator_list", None):
            start = node.decorator_list[0].lineno - 1
        chunks.append("".join(lines[start:node.end_lineno]))
found = set()
for node in tree.body:
    if isinstance(node, ast.FunctionDef):
        found.add(node.name)
    elif isinstance(node, ast.Assign):
        found.update(t.id for t in node.targets if isinstance(t, ast.Name))
missing = set(KEEP_FROM_NORMALIZE) - found
assert not missing, missing

stub = ('"""Money and date helpers (sandbox copy).\n\n'
        'Only the parsing/formatting helpers the generator and loader import are\n'
        'present here, copied verbatim. Everything else in the real module is\n'
        'deliberately absent from this sandbox."""\n\n'
        "from __future__ import annotations\n\n"
        "from datetime import date, datetime, timedelta\n\n\n"
        + "\n\n".join(c.rstrip("\n") + "\n" for c in chunks))
open(f"{SB}/src/recon/normalize.py", "w", encoding="utf-8", newline="\n").write(stub)

open(f"{SB}/pytest.ini", "w", encoding="utf-8", newline="\n").write(
    "[pytest]\ntestpaths = tests\npythonpath = src\naddopts = -q -p no:cacheprovider\n")
print("sandbox at", SB)
print(sorted(os.listdir(f"{SB}/src/recon")), sorted(os.listdir(f"{SB}/tests")))
