from __future__ import annotations

import copy
import re
from dataclasses import dataclass


@dataclass
class ReconcileResult:
    timeline_events: list[dict]
    death_events: list[dict]
    final_team_state: dict


UNKNOWN_LOCATION_VALUES = {"", "unknown", "???", "n/a", "none", "null", "unk"}


def _uniq_names(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        name = str(item).strip()
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(name)
    return out


def _location_key(value: str) -> str:
    return re.sub(r"\s+", " ", str(value).strip().lower())


def _is_unknown_location(value: str) -> bool:
    return _location_key(value) in UNKNOWN_LOCATION_VALUES


def _safe_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _location_confidence(event: dict) -> float:
    source = str(event.get("source", "")).strip().lower()
    if source == "sidecar":
        return 1.0
    confidence = _safe_float(event.get("confidence", 0.0), 0.0)
    if confidence <= 0.0 and str(event.get("location", "")).strip():
        confidence = 0.50
    return max(0.0, min(1.0, confidence))


def _build_location_reasoning(event: dict, confidence: float) -> str:
    reasoning = str(event.get("reasoning", "")).strip()
    if reasoning:
        return reasoning
    source = str(event.get("source", "ocr")).strip() or "ocr"
    if source == "sidecar":
        return "Location set from sidecar event."
    return f"Location inferred from {source} with confidence {confidence:.2f}."


def _should_replace_location(
    current_location: str,
    current_confidence: float,
    candidate_location: str,
    candidate_confidence: float,
    source: str,
) -> bool:
    if _is_unknown_location(candidate_location):
        return False

    if source == "sidecar":
        return True

    if _is_unknown_location(current_location):
        return candidate_confidence >= 0.40

    if _location_key(current_location) == _location_key(candidate_location):
        return candidate_confidence > current_confidence + 0.03

    if candidate_confidence >= current_confidence + 0.10:
        return True

    if len(candidate_location.strip()) > len(current_location.strip()) + 4 and candidate_confidence >= current_confidence - 0.03:
        return True

    return candidate_confidence >= 0.90


def _apply_location_update(state: dict, event: dict, location: str) -> None:
    source = str(event.get("source", "ocr")).strip().lower() or "ocr"
    candidate_confidence = _location_confidence(event)
    current_location = str(state.get("location", "Unknown")).strip() or "Unknown"
    current_confidence = _safe_float(state.get("location_confidence", 0.0), 0.0)

    if _should_replace_location(
        current_location=current_location,
        current_confidence=current_confidence,
        candidate_location=location,
        candidate_confidence=candidate_confidence,
        source=source,
    ):
        state["location"] = location
        state["location_confidence"] = candidate_confidence
        state["location_reasoning"] = _build_location_reasoning(event, candidate_confidence)


def _event_summary(event: dict) -> str:
    event_type = str(event.get("type", "")).strip().lower()
    pokemon = str(event.get("pokemon", "")).strip()
    location = str(event.get("location", "")).strip()

    if event_type == "catch":
        if pokemon and location:
            return f"Caught {pokemon} at {location}."
        if pokemon:
            return f"Caught {pokemon}."
        return "Catch event detected."
    if event_type == "death":
        if pokemon:
            return f"{pokemon} fainted."
        return "A Pokemon fainted."
    if event_type == "swap_to_team":
        return f"Swapped {pokemon} into team." if pokemon else "Swap into team."
    if event_type == "move_to_box":
        return f"Moved {pokemon} to PC box." if pokemon else "Moved a Pokemon to PC box."
    if event_type == "location":
        return f"Entered {location}." if location else "Location update detected."
    return "Timeline updated."


def reconcile_events(sidecar_events: list[dict], ocr_events: list[dict], initial_state: dict) -> ReconcileResult:
    merged = sorted(sidecar_events + ocr_events, key=lambda e: e.get("timestamp", 0.0))

    deduped: list[dict] = []
    for event in merged:
        duplicate = False
        for existing in deduped:
            same_type = existing.get("type") == event.get("type")
            close_time = abs(existing.get("timestamp", 0.0) - event.get("timestamp", 0.0)) <= 5.0
            same_pokemon = str(existing.get("pokemon", "")).strip().lower() == str(event.get("pokemon", "")).strip().lower()
            same_location = _location_key(str(existing.get("location", ""))) == _location_key(str(event.get("location", "")))
            if not (same_type and close_time):
                continue

            if event.get("type") == "location" and same_location:
                if existing.get("source") == "ocr" and event.get("source") == "sidecar":
                    existing.update(event)
                elif _location_confidence(event) > _location_confidence(existing):
                    existing.update(event)
                duplicate = True
                break

            if event.get("type") != "location" and same_pokemon:
                if existing.get("source") == "ocr" and event.get("source") == "sidecar":
                    existing.update(event)
                duplicate = True
                break
        if not duplicate:
            deduped.append(event)

    state = copy.deepcopy(initial_state)
    state.setdefault("active_team", [])
    state.setdefault("graveyard", [])
    state.setdefault("pc_box", [])
    state.setdefault("master", "J")
    state.setdefault("catches", 0)
    state.setdefault("location", "Unknown")
    state.setdefault("location_confidence", 0.0 if _is_unknown_location(str(state.get("location", ""))) else 0.65)
    state.setdefault("location_reasoning", "No strong location evidence yet.")
    state.setdefault("event_count", 0)
    state.setdefault("last_event", "No timeline events yet.")
    state.setdefault("last_event_source", "none")
    state.setdefault("last_event_timestamp", 0.0)
    state["active_team"] = _uniq_names(list(state["active_team"]))
    state["graveyard"] = _uniq_names(list(state["graveyard"]))
    state["pc_box"] = _uniq_names(list(state["pc_box"]))

    deaths: list[dict] = []

    for event in deduped:
        event_type = event.get("type", "")
        pokemon = event.get("pokemon", "")
        location = event.get("location", "")

        if event_type == "catch":
            state["catches"] = int(state.get("catches", 0)) + 1
            if pokemon and pokemon not in state["active_team"] and len(state["active_team"]) < 6:
                state["active_team"].append(pokemon)
            elif pokemon and pokemon not in state["pc_box"]:
                state["pc_box"].append(pokemon)
            if location:
                _apply_location_update(state, event, location)

        elif event_type == "swap_to_team":
            if pokemon and pokemon in state["pc_box"]:
                state["pc_box"].remove(pokemon)
            if pokemon and pokemon not in state["active_team"] and len(state["active_team"]) < 6:
                state["active_team"].append(pokemon)

        elif event_type == "move_to_box":
            if pokemon and pokemon in state["active_team"]:
                state["active_team"].remove(pokemon)
            if pokemon and pokemon not in state["pc_box"]:
                state["pc_box"].append(pokemon)

        elif event_type == "death":
            if pokemon and pokemon in state["active_team"]:
                state["active_team"].remove(pokemon)
            if pokemon and pokemon not in state["graveyard"]:
                state["graveyard"].append(pokemon)
            deaths.append(event)

        elif event_type == "location":
            if location:
                _apply_location_update(state, event, location)

        state["active_team"] = _uniq_names(list(state["active_team"]))[:6]
        state["graveyard"] = _uniq_names(list(state["graveyard"]))
        state["pc_box"] = _uniq_names(list(state["pc_box"]))
        state["fallen_count"] = len(state["graveyard"])
        state["event_count"] = int(state.get("event_count", 0)) + 1
        state["last_event"] = _event_summary(event)
        state["last_event_source"] = str(event.get("source", "unknown")).strip() or "unknown"
        state["last_event_timestamp"] = _safe_float(event.get("timestamp", 0.0), 0.0)

        event["team_state_snapshot"] = copy.deepcopy(state)

    state["fallen_count"] = len(state["graveyard"])

    return ReconcileResult(
        timeline_events=deduped,
        death_events=deaths,
        final_team_state=state,
    )
