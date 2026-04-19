from __future__ import annotations

import json
import re
from pathlib import Path

from src.config import EditorConfig
from src.events.ocr_detector import detect_auto_events_ocr
from src.events.reconciler import reconcile_events
from src.events.sidecar_parser import SidecarDirectives, load_sidecar_bundle
from src.ingest.dedupe import Fingerprint, build_fingerprint, similarity_score
from src.logging.death_log import DeathLogEntry, append_deaths, load_death_log
from src.ingest.scanner import discover_raw_videos
from src.logging.run_log import RunEntry, append_entry, load_log, utc_now_iso
from src.logging.state_log import append_video_timeline
from src.overlay.compositor import prefetch_pokemon_icons
from src.render.encoder import probe_duration, render_video


def _looks_like_placeholder_name(name: str) -> bool:
    return bool(re.fullmatch(r"pokemon\s*\d+", name.strip(), flags=re.IGNORECASE))


def _drop_placeholder_team(team: list[str]) -> list[str]:
    if not team:
        return []
    placeholder_count = sum(1 for member in team if _looks_like_placeholder_name(member))
    if placeholder_count == len(team):
        return []
    return team


def _load_team_state(path: Path) -> dict:
    default_state = {
        "active_team": [],
        "graveyard": [],
        "pc_box": [],
        "master": "J",
        "catches": 0,
        "location": "Unknown",
    }

    if not path.exists():
        return default_state

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default_state

    if not isinstance(data, dict):
        return default_state

    normalized = dict(default_state)
    normalized.update(data)
    normalized["active_team"] = _drop_placeholder_team(
        [str(x).strip() for x in normalized.get("active_team", []) if str(x).strip()]
    )
    normalized["graveyard"] = [str(x).strip() for x in normalized.get("graveyard", []) if str(x).strip()]
    normalized["pc_box"] = [str(x).strip() for x in normalized.get("pc_box", []) if str(x).strip()]
    normalized["master"] = str(normalized.get("master", "J")).strip() or "J"
    normalized["location"] = str(normalized.get("location", "Unknown")).strip() or "Unknown"
    normalized["catches"] = int(normalized.get("catches", 0))
    normalized["event_count"] = int(normalized.get("event_count", 0))
    normalized["last_event"] = str(normalized.get("last_event", "No timeline events yet.")).strip() or "No timeline events yet."
    normalized["location_confidence"] = float(normalized.get("location_confidence", 0.0))
    return normalized


def _save_team_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _merge_lifetime_graveyard(team_state: dict, death_log_path: Path) -> dict:
    death_log = load_death_log(death_log_path)
    names: list[str] = []
    for entry in death_log.get("entries", []):
        if not isinstance(entry, dict):
            continue
        pokemon = str(entry.get("pokemon", "")).strip()
        if pokemon and pokemon.upper() != "UNKNOWN":
            names.append(pokemon)

    merged = list(team_state.get("graveyard", [])) + names
    unique: list[str] = []
    seen: set[str] = set()
    for name in merged:
        clean = str(name).strip()
        key = clean.lower()
        if not clean or key in seen:
            continue
        seen.add(key)
        unique.append(clean)

    team_state["graveyard"] = unique
    team_state["fallen_count"] = len(unique)
    return team_state


def _seed_starting_team(team_state: dict, directives: SidecarDirectives) -> dict:
    seeded_state = dict(team_state)
    existing_team = [str(x).strip() for x in seeded_state.get("active_team", []) if str(x).strip()]
    if existing_team:
        return seeded_state

    if not directives.starting_team:
        return seeded_state

    graveyard = {str(x).strip().lower() for x in seeded_state.get("graveyard", []) if str(x).strip()}
    seeded_team: list[str] = []
    seen: set[str] = set()
    for raw_name in directives.starting_team:
        name = str(raw_name).strip()
        if not name:
            continue
        key = name.lower()
        if key in seen or key in graveyard:
            continue
        seen.add(key)
        seeded_team.append(name)

    if seeded_team:
        seeded_state["active_team"] = seeded_team[:6]
        seeded_state["last_event"] = "Seeded team from sidecar directives."
        seeded_state["last_event_source"] = "sidecar"

    return seeded_state


def _prepare_video_initial_state(team_state: dict) -> dict:
    prepared = dict(team_state)
    prepared["location"] = "Unknown"
    prepared["location_confidence"] = 0.0
    prepared["location_reasoning"] = "No strong location evidence yet."
    prepared["event_count"] = 0
    prepared["last_event"] = "No timeline events yet."
    prepared["last_event_source"] = "none"
    prepared["last_event_timestamp"] = 0.0
    return prepared


def _collect_pokemon_names_for_icons(
    initial_state: dict,
    timeline_events: list[dict],
    final_state: dict,
) -> list[str]:
    names: list[str] = []

    def add_state_names(state: dict) -> None:
        for key in ("active_team", "graveyard", "pc_box"):
            for raw_name in state.get(key, []):
                name = str(raw_name).strip()
                if name:
                    names.append(name)

    add_state_names(initial_state)
    add_state_names(final_state)

    for event in timeline_events:
        name = str(event.get("pokemon", "")).strip()
        if name:
            names.append(name)

    unique: list[str] = []
    seen: set[str] = set()
    for name in names:
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(name)
    return unique


def _find_duplicate_confidence(log_data: dict, file_fp_sha256: str, file_fp_perceptual: str) -> float:
    candidate = Fingerprint(sha256=file_fp_sha256, perceptual=file_fp_perceptual)
    max_confidence = 0.0

    for entry in log_data.get("entries", []):
        existing = Fingerprint(
            sha256=str(entry.get("sha256", "")),
            perceptual=str(entry.get("perceptual_hash", "")),
        )
        score = similarity_score(candidate, existing)
        max_confidence = max(max_confidence, score)

    return max_confidence


def run_batch(config: EditorConfig, force: bool = False) -> None:
    videos = discover_raw_videos(config.raw_dir)
    if not videos:
        print("No raw videos found in data/raw_footage.")
        return

    team_state = _load_team_state(config.team_state_path)
    team_state = _merge_lifetime_graveyard(team_state, config.dead_pokemon_log_path)
    _save_team_state(config.team_state_path, team_state)

    for video_path in videos:
        print(f"Processing {video_path.name}...")

        try:
            fingerprint = build_fingerprint(video_path)
            log_data = load_log(config.processed_log_path)
            duplicate_confidence = _find_duplicate_confidence(
                log_data, fingerprint.sha256, fingerprint.perceptual
            )

            if not force and duplicate_confidence >= config.duplicate_threshold:
                append_entry(
                    config.processed_log_path,
                    RunEntry(
                        input_file=video_path.name,
                        output_file="",
                        status="skipped_duplicate",
                        sha256=fingerprint.sha256,
                        perceptual_hash=fingerprint.perceptual,
                        duplicate_confidence=duplicate_confidence,
                        notes="Auto-skipped likely duplicate based on fingerprint similarity.",
                        created_at=utc_now_iso(),
                    ),
                )
                print(
                    f"Skipped {video_path.name} (duplicate confidence {duplicate_confidence:.2f})."
                )
                continue

            sidecar_events, directives = load_sidecar_bundle(config.events_dir, video_path)
            team_state_for_video = _prepare_video_initial_state(team_state)
            team_state_for_video = _seed_starting_team(team_state_for_video, directives)

            ocr_interval = float(config.ocr_interval_seconds)
            try:
                video_duration = probe_duration(video_path, ffprobe_bin=config.ffprobe_bin)
            except Exception:
                video_duration = 0.0

            file_size_mb = 0.0
            try:
                file_size_mb = float(video_path.stat().st_size) / (1024.0 * 1024.0)
            except Exception:
                file_size_mb = 0.0

            if video_duration >= 5400.0:
                ocr_interval = max(ocr_interval, 10.0)
            elif video_duration >= 3600.0:
                ocr_interval = max(ocr_interval, 7.5)
            elif video_duration >= 1800.0:
                ocr_interval = max(ocr_interval, 4.5)
            elif video_duration <= 0.0:
                if file_size_mb >= 450.0:
                    ocr_interval = max(ocr_interval, 10.0)
                elif file_size_mb >= 300.0:
                    ocr_interval = max(ocr_interval, 7.5)
                elif file_size_mb >= 180.0:
                    ocr_interval = max(ocr_interval, 4.5)

            if ocr_interval > float(config.ocr_interval_seconds):
                print(f"Using adaptive OCR interval {ocr_interval:.1f}s for {video_path.name}.")

            ocr_events = detect_auto_events_ocr(
                video_path,
                sample_every_seconds=ocr_interval,
                duration_hint_seconds=video_duration if video_duration > 0.0 else None,
                enable_web_lookup=config.enable_location_web_lookup,
                web_lookup_timeout_seconds=config.location_web_lookup_timeout_seconds,
                location_corrections=directives.location_corrections,
                pokemon_corrections=directives.pokemon_corrections,
                location_consensus_hits=config.location_consensus_hits,
                location_consensus_window_seconds=config.location_consensus_window_seconds,
            )
            reconcile_result = reconcile_events(sidecar_events, ocr_events, team_state_for_video)

            icon_candidates = _collect_pokemon_names_for_icons(
                team_state_for_video,
                reconcile_result.timeline_events,
                reconcile_result.final_team_state,
            )
            icon_summary = prefetch_pokemon_icons(config, icon_candidates)
            requested = int(icon_summary.get("requested", 0))
            resolved = int(icon_summary.get("resolved", 0))
            missing = list(icon_summary.get("missing", []))
            if requested > 0:
                print(f"Icon prefetch resolved {resolved}/{requested} Pokemon icons.")
            if missing:
                print(f"Icon prefetch missing: {', '.join(str(x) for x in missing)}")

            output_name = f"{video_path.stem}.final.mp4"
            output_path = config.output_dir / output_name

            render_video(
                config=config,
                input_video=video_path,
                output_video=output_path,
                initial_state=team_state_for_video,
                timeline_events=reconcile_result.timeline_events,
                death_events=reconcile_result.death_events,
            )

            team_state = reconcile_result.final_team_state
            _save_team_state(config.team_state_path, team_state)
            append_video_timeline(
                config.state_timeline_log_path,
                video_file=video_path.name,
                timeline_events=reconcile_result.timeline_events,
                final_state=team_state,
                logged_at=utc_now_iso(),
            )

            death_entries: list[DeathLogEntry] = []
            for event in reconcile_result.death_events:
                pokemon_name = str(event.get("pokemon", "")).strip() or "UNKNOWN"
                death_entries.append(
                    DeathLogEntry(
                        pokemon=pokemon_name,
                        timestamp=float(event.get("timestamp", 0.0)),
                        video_file=video_path.name,
                        source=str(event.get("source", "unknown")),
                        logged_at=utc_now_iso(),
                    )
                )
            added_deaths = append_deaths(config.dead_pokemon_log_path, death_entries)

            append_entry(
                config.processed_log_path,
                RunEntry(
                    input_file=video_path.name,
                    output_file=output_name,
                    status="processed",
                    sha256=fingerprint.sha256,
                    perceptual_hash=fingerprint.perceptual,
                    duplicate_confidence=duplicate_confidence,
                    notes=(
                        f"Processed with {len(reconcile_result.death_events)} death events "
                        f"({added_deaths} appended to dead_pokemon_log)."
                    ),
                    created_at=utc_now_iso(),
                ),
            )
            print(f"Finished {video_path.name} -> {output_name}")

        except Exception as exc:
            append_entry(
                config.processed_log_path,
                RunEntry(
                    input_file=video_path.name,
                    output_file="",
                    status="failed",
                    sha256="",
                    perceptual_hash="",
                    duplicate_confidence=0.0,
                    notes=f"Error: {exc}",
                    created_at=utc_now_iso(),
                ),
            )
            print(f"Failed {video_path.name}: {exc}")
