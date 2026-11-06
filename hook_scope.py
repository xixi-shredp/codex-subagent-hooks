"""Installation scope, separate from the editable communication policy."""
import json
from pathlib import Path


def project_root():
    base = Path(__file__).resolve().parent
    value = json.loads((base / "installation.json").read_text(encoding="utf-8"))
    if value == {"mode": "global"}:
        return None
    if (isinstance(value, dict) and set(value) == {"mode", "project"}
            and value["mode"] == "local" and isinstance(value["project"], str)
            and value["project"]):
        return (base / value["project"]).resolve()
    raise ValueError("invalid installation scope")
