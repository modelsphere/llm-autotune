#!/usr/bin/env python3
"""Bring the vendored model-catalog schemas up to the catalog's.

The catalog export validates what it writes against a copy of the catalog's
own schemas (app/control/catalog_export/schema/), so it works with no network
and a bad file fails here rather than in the catalog's CI. The copy goes
stale whenever the catalog's schema moves; this is the one command that
refreshes it, and the check that says when it has.

    scripts/sync_catalog_schema.py                 copy from the catalog's master
    scripts/sync_catalog_schema.py --ref <commit>  ... from a commit, branch or tag
    scripts/sync_catalog_schema.py --from <dir>    ... from a local checkout
    scripts/sync_catalog_schema.py --check         change nothing; exit 1 and
                                                   list the files that differ

Run the tests after a sync: a schema that only gained an optional field
passes as it is, and one that tightened a rule shows up there. A new field
the export should write is a change to the export, not to this copy.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.request
from pathlib import Path

REPO = "modelsphere/model-catalog"
SCHEMA_DIR = Path(__file__).resolve().parents[1] / "app/control/catalog_export/schema"
FILES = (
    "metadata.schema.json",
    "metadata.v2.schema.json",
    "variant.schema.json",
    "version.schema.json",
)
CONFIG = "schema/crds/config.json"  # holds minimumChartVersions


def _commit(ref: str) -> str:
    url = f"https://api.github.com/repos/{REPO}/commits/{ref}"
    request = urllib.request.Request(url, headers={"Accept": "application/vnd.github.sha"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode().strip()


def _remote(commit: str, path: str) -> str:
    url = f"https://raw.githubusercontent.com/{REPO}/{commit}/{path}"
    with urllib.request.urlopen(url, timeout=30) as response:
        return response.read().decode()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--ref", default="master")
    parser.add_argument("--from", dest="checkout", type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    if args.checkout:
        commit = subprocess.run(
            ["git", "-C", str(args.checkout), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

        def read(path: str) -> str:
            return (args.checkout / path).read_text()
    else:
        commit = _commit(args.ref)

        def read(path: str) -> str:
            return _remote(commit, path)

    wanted = {name: read(f"schema/{name}") for name in FILES}
    pins = json.loads((SCHEMA_DIR / "pins.json").read_text())
    minimums = json.loads(read(CONFIG))["minimumChartVersions"]
    new_pins = {
        **pins,
        "source": f"github.com/{REPO}@{commit}",
        "minimumChartVersions": minimums,
    }

    changed = [name for name, text in wanted.items() if (SCHEMA_DIR / name).read_text() != text]
    if pins.get("minimumChartVersions") != minimums:
        changed.append("pins.json (minimumChartVersions)")
    if args.check:
        if changed:
            print(f"behind {REPO}@{commit[:7]}: " + ", ".join(changed))
            print("run backend/scripts/sync_catalog_schema.py, then the tests")
            return 1
        print(f"up to date with {REPO}@{commit[:7]}")
        return 0
    for name, text in wanted.items():
        (SCHEMA_DIR / name).write_text(text)
    (SCHEMA_DIR / "pins.json").write_text(json.dumps(new_pins, indent=2) + "\n")
    print(f"synced to {REPO}@{commit[:7]}: " + (", ".join(changed) or "nothing changed"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
