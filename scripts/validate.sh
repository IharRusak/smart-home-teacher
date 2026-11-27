#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
python3 - "$ROOT" <<'PY'
import ast
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
component = root / "custom_components" / "smart_home_teacher"

required = [
    "manifest.json", "const.py", "config_flow.py",
    "__init__.py", "conversation.py", "custom_actions.py",
    "translations/en.json", "translations/ru.json",
]
missing = [name for name in required if not (component / name).is_file()]
if missing:
    raise SystemExit(f"Missing integration files: {missing}")

for path in component.rglob("*.py"):
    ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    print(f"Python syntax OK: {path.relative_to(root)}")

for path in root.rglob("*.json"):
    json.loads(path.read_text(encoding="utf-8"))
    print(f"JSON OK: {path.relative_to(root)}")

manifest = json.loads((component / "manifest.json").read_text(encoding="utf-8"))
assert manifest["domain"] == "smart_home_teacher"
assert manifest["version"] == "1.2.1"
assert manifest["config_flow"] is True
print("Integration manifest checks OK")
print("Static validation passed. Runtime HA/Ollama tests are not performed.")
PY
