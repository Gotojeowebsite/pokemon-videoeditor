from __future__ import annotations

from pathlib import Path


def build_audio_expression_for_windows(death_events: list[dict], low_volume: float = 0.3) -> str:
    if not death_events:
        return "1"

    terms: list[str] = []
    for event in death_events:
        start = float(event.get("timestamp", 0.0))
        end = start + 4.0
        terms.append(f"between(t\\,{start:.3f}\\,{end:.3f})")

    if_expr = "+".join(terms)
    return f"if({if_expr}\\,{low_volume}\\,1)"


def build_sad_music_volume_expression(death_events: list[dict], sad_volume: float = 0.55) -> str:
    if not death_events:
        return "0"

    terms: list[str] = []
    for event in death_events:
        start = float(event.get("timestamp", 0.0))
        end = start + 4.0
        terms.append(f"between(t\\,{start:.3f}\\,{end:.3f})")

    if_expr = "+".join(terms)
    return f"if({if_expr}\\,{sad_volume}\\,0)"


def can_use_sad_music(path: Path) -> bool:
    return path.exists() and path.is_file()
