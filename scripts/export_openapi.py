#!/usr/bin/env python
"""Freeze the OpenAPI document so contract drift shows up as a diff in CI."""
import json
from pathlib import Path

from rec.api.app import app

out = Path(__file__).resolve().parents[1] / "contracts" / "openapi.json"
out.write_text(json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n")
print(out)
