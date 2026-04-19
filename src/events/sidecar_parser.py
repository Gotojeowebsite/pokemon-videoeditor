from __future__ import annotations

from dataclasses import dataclass
import json
import re
from pathlib import Path


ALLOWED_EVENT_TYPES = {"catch", "death", "swap_to_team", "move_to_box", "location"}
POKEMON_REQUIRED = {"catch", "death", "swap_to_team", "move_to_box"}


@dataclass(frozen=True)
class SidecarDirectives:
    location_corrections: dict[str, str]
    pokemon_corrections: dict[str, str]
    starting_team: list[str]


def _normalize_location_key(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9\s]+", " ", str(value).strip().lower())
    return re.sub(r"\s+", " ", normalized).strip()


def _empty_directives() -> SidecarDirectives:
    return SidecarDirectives(
        location_corrections={},
        pokemon_corrections={},
        starting_team=[],
    )


def sidecar_path_for_video(events_dir: Path, video_path: Path) -> Path:
    return events_dir / f"{video_path.stem}.events.json"


def _read_sidecar_payload(sidecar: Path) -> dict:
    if not sidecar.exists():
        return {}

    try:
        data = json.loads(sidecar.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}

    if not isinstance(data, dict):
        return {}
    return data


def _parse_events(data: dict) -> list[dict]:
    events = data.get("events", [])
    if not isinstance(events, list):
        return []

    normalized: list[dict] = []
    for raw_event in events:
        if not isinstance(raw_event, dict):
            continue
        try:
            timestamp = float(raw_event.get("timestamp", 0.0))
        except (TypeError, ValueError):
            timestamp = 0.0
        event_type = str(raw_event.get("type", "")).strip().lower()
        pokemon = str(raw_event.get("pokemon", "")).strip()
        location = str(raw_event.get("location", "")).strip()

        if event_type not in ALLOWED_EVENT_TYPES:
            continue
        if event_type in POKEMON_REQUIRED and not pokemon:
            continue

        normalized.append(
            {
                "timestamp": max(0.0, timestamp),
                "type": event_type,
                "pokemon": pokemon,
                "location": location,
                "source": "sidecar",
            }
        )

    return sorted(normalized, key=lambda x: x["timestamp"])


def _parse_corrections(raw_mapping: object, *, location_keys: bool) -> dict[str, str]:
    if not isinstance(raw_mapping, dict):
        return {}

    parsed: dict[str, str] = {}
    for raw_key, raw_value in raw_mapping.items():
        key = str(raw_key).strip()
        value = str(raw_value).strip()
        if not key or not value:
            continue
        norm_key = _normalize_location_key(key) if location_keys else key.lower()
        parsed[norm_key] = value
    return parsed


def _parse_starting_team(raw_team: object) -> list[str]:
    if not isinstance(raw_team, list):
        return []

    out: list[str] = []
    seen: set[str] = set()
    for raw in raw_team:
        name = str(raw).strip()
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(name)
    return out[:6]


def _parse_directives(data: dict) -> SidecarDirectives:
    directives = data.get("directives", {})
    if not isinstance(directives, dict):
        directives = {}

    location_raw = directives.get("location_corrections", data.get("location_corrections", {}))
    pokemon_raw = directives.get("pokemon_corrections", data.get("pokemon_corrections", {}))
    starting_team_raw = directives.get("starting_team", data.get("starting_team", []))

    return SidecarDirectives(
        location_corrections=_parse_corrections(location_raw, location_keys=True),
        pokemon_corrections=_parse_corrections(pokemon_raw, location_keys=False),
        starting_team=_parse_starting_team(starting_team_raw),
    )


def load_sidecar_bundle(events_dir: Path, video_path: Path) -> tuple[list[dict], SidecarDirectives]:
    sidecar = sidecar_path_for_video(events_dir, video_path)
    data = _read_sidecar_payload(sidecar)
    if not data:
        return [], _empty_directives()

    return _parse_events(data), _parse_directives(data)


def load_sidecar_events(events_dir: Path, video_path: Path) -> list[dict]:
    events, _ = load_sidecar_bundle(events_dir, video_path)
    return events


def load_sidecar_directives(events_dir: Path, video_path: Path) -> SidecarDirectives:
    _, directives = load_sidecar_bundle(events_dir, video_path)
    return directives
