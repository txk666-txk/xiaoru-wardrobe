"""Validate one or more JSON files with the project's Python runtime."""

import json
import sys


if len(sys.argv) < 2:
    print("Usage: validate_json.py FILE [FILE ...]", file=sys.stderr)
    raise SystemExit(2)

for filename in sys.argv[1:]:
    try:
        with open(filename, "r", encoding="utf-8") as handle:
            json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        print(f"Invalid JSON: {filename}: {exc}", file=sys.stderr)
        raise SystemExit(1)

print("JSON valid:", ", ".join(sys.argv[1:]))
