#!/usr/bin/env python
"""EVT-004 in CI: every schema in contracts/avro must stay BACKWARD_TRANSITIVE compatible
with every version of itself ever committed. Needs full git history (fetch-depth: 0).

A breaking change is not "fixed" here: it gets a new schema name (a new contract) and a
consumer migration, as the SDD requires.
"""
from __future__ import annotations

import subprocess
import sys

from rec.contracts import AVRO_DIR, backward_compatible, parse


def history(path: str) -> list[tuple[str, str]]:
    commits = subprocess.run(["git", "log", "--format=%h", "--", path],
                             capture_output=True, text=True, check=True).stdout.split()
    return [(c, subprocess.run(["git", "show", f"{c}:{path}"], capture_output=True,
                               text=True, check=True).stdout) for c in commits]


def main() -> int:
    failures = 0
    for current in sorted(AVRO_DIR.glob("*.avsc")):
        path = str(current.relative_to(AVRO_DIR.parents[1]))
        new = parse(current.read_text())
        versions = history(path)
        for commit, text in versions:
            error = backward_compatible(parse(text), new)
            if error:
                failures += 1
                print(f"INCOMPATIBLE {path} vs {commit}: {error}")
        print(f"{path}: checked against {len(versions)} committed version(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
