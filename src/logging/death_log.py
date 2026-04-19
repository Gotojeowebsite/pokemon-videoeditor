from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class DeathLogEntry:
    pokemon: str
    timestamp: float
    video_file: str
    source: str
    logged_at: str


def _default_payload() -> dict[str, Any]:
    return {"version": 1, "entries": []}


def load_death_log(path: Path) -> dict[str, Any]:
    if not path.exists():
        return _default_payload()

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return _default_payload()

    if not isinstance(data, dict):
        return _default_payload()

    if not isinstance(data.get("entries"), list):
        data["entries"] = []
    if "version" not in data:
        data["version"] = 1

    return data


def save_death_log(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp_path.replace(path)


def append_deaths(path: Path, entries: list[DeathLogEntry]) -> int:
    if not entries:
        return 0

    data = load_death_log(path)
    existing = data["entries"]

    added = 0
    for entry in entries:
        is_duplicate = False
        for old in existing:
            same_video = str(old.get("video_file", "")) == entry.video_file
            same_name = str(old.get("pokemon", "")).strip().lower() == entry.pokemon.strip().lower()
            old_ts = float(old.get("timestamp", -9999.0))
            close_ts = abs(old_ts - entry.timestamp) <= 1.0
            if same_video and same_name and close_ts:
                is_duplicate = True
                break
        if is_duplicate:
            continue
        existing.append(entry.__dict__)
        added += 1

    save_death_log(path, data)
    return added
