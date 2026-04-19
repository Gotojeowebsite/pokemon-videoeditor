from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass
class RunEntry:
    input_file: str
    output_file: str
    status: str
    sha256: str
    perceptual_hash: str
    duplicate_confidence: float
    notes: str
    created_at: str


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_payload() -> dict[str, Any]:
    return {"version": 1, "entries": []}


def load_log(path: Path) -> dict[str, Any]:
    if not path.exists():
        return _default_payload()

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return _default_payload()

    if not isinstance(data, dict):
        return _default_payload()

    entries = data.get("entries")
    if not isinstance(entries, list):
        data["entries"] = []

    if "version" not in data:
        data["version"] = 1

    return data


def save_log(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp_path.replace(path)


def append_entry(path: Path, entry: RunEntry) -> None:
    data = load_log(path)
    data["entries"].append(entry.__dict__)
    save_log(path, data)
