#!/usr/bin/env python3
"""Check whether an <output_dir>/state/specs/ directory holds complete,
generation-ready OperationSpec JSON files.

Exits 0 if ready, 1 with a punch list if not.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REQUIRED_OP = ("operation_id", "input_fields", "output_fields")
REQUIRED_FIELD = ("name", "field_type")


def check(state_dir: Path) -> list[str]:
    specs_dir = state_dir / "specs"
    issues: list[str] = []
    if not specs_dir.is_dir():
        return [f"missing {specs_dir}"]
    files = list(specs_dir.glob("*.json"))
    if not files:
        return [f"no operation specs in {specs_dir}"]
    for p in files:
        try:
            data = json.loads(p.read_text())
        except Exception as e:
            issues.append(f"{p.name}: not valid JSON ({e})")
            continue
        for k in REQUIRED_OP:
            if not data.get(k):
                issues.append(f"{p.name}: missing '{k}'")
        for role in ("input_fields", "output_fields"):
            for i, f in enumerate(data.get(role) or []):
                if not isinstance(f, dict):
                    issues.append(f"{p.name}: {role}[{i}] not an object")
                    continue
                for k in REQUIRED_FIELD:
                    if not f.get(k):
                        issues.append(
                            f"{p.name}: {role}[{i}].{k} missing "
                            f"(field name: {f.get('name', '?')})"
                        )
        tools = data.get("tools") or []
        if tools and not any(t.get("role") == "primary" for t in tools if isinstance(t, dict)):
            issues.append(f"{p.name}: no tool with role='primary' in tools[]")
    if not (state_dir / "infrastructure_schema.json").is_file():
        issues.append("missing state/infrastructure_schema.json")
    if not (state_dir / "session_flow_config.json").is_file():
        issues.append("missing state/session_flow_config.json")
    return issues


def main() -> int:
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <output_dir>", file=sys.stderr)
        return 2
    out = Path(sys.argv[1]).resolve()
    issues = check(out / "state")
    if not issues:
        print(f"OK — specs in {out}/state/ are generation-ready")
        return 0
    print(f"INCOMPLETE — {len(issues)} issue(s) in {out}/state/:\n")
    for i in issues:
        print(f"  - {i}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
