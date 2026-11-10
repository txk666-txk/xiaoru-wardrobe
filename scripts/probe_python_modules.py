"""Check that the Fay runtime can import the modules required by the launcher."""

import importlib.util
import sys


required = sys.argv[1:]
missing = []
for name in required:
    try:
        available = importlib.util.find_spec(name) is not None
    except (ModuleNotFoundError, ImportError, ValueError):
        available = False
    if not available:
        missing.append(name)

print("Python:", sys.executable)
print("Missing modules:", ", ".join(missing) if missing else "none")
raise SystemExit(1 if missing else 0)
