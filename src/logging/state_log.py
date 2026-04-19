from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _default_payload() -> dict[str, Any]:
    return {"version": 1, "videos": []}


def load_state_log(path: Path) -> dict[str, Any]:
    if not path.exists():
        return _default_payload()

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return _default_payload()

    if not isinstance(data, dict):
        return _default_payload()

    videos = data.get("videos")
    if not isinstance(videos, list):
        data["videos"] = []

    if "version" not in data:
        data["version"] = 1

    return data


def save_state_log(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp_path.replace(path)


def append_video_timeline(
    path: Path,
    *,
    video_file: str,
    timeline_events: list[dict],
    final_state: dict,
    logged_at: str,
) -> None:
    payload = load_state_log(path)

    compact_events: list[dict[str, Any]] = []
    for event in timeline_events:
        snapshot = event.get("team_state_snapshot", {})
        if not isinstance(snapshot, dict):
            snapshot = {}

        compact_events.append(
            {
                "timestamp": float(event.get("timestamp", 0.0)),
                "type": str(event.get("type", "")),
                "pokemon": str(event.get("pokemon", "")),
                "location": str(event.get("location", "")),
                "source": str(event.get("source", "")),
                "snapshot": {
                    "catches": int(snapshot.get("catches", 0)),
                    "fallen_count": int(snapshot.get("fallen_count", len(snapshot.get("graveyard", [])))),
                    "location": str(snapshot.get("location", "Unknown")),
                    "location_confidence": float(snapshot.get("location_confidence", 0.0)),
                    "location_reasoning": str(snapshot.get("location_reasoning", "")),
                    "event_count": int(snapshot.get("event_count", 0)),
                    "last_event": str(snapshot.get("last_event", "")),
                    "last_event_source": str(snapshot.get("last_event_source", "")),
                    "last_event_timestamp": float(snapshot.get("last_event_timestamp", 0.0)),
                    "pc_box": list(snapshot.get("pc_box", [])),
                    "graveyard": list(snapshot.get("graveyard", [])),
                    "active_team": list(snapshot.get("active_team", [])),
                },
            }
        )

    payload["videos"] = [v for v in payload["videos"] if str(v.get("video_file", "")) != video_file]
    payload["videos"].append(
        {
            "video_file": video_file,
            "logged_at": logged_at,
            "events": compact_events,
            "final_state": final_state,
        }
    )
    save_state_log(path, payload)
