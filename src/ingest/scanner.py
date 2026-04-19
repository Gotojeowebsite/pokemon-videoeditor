from __future__ import annotations

from pathlib import Path

from src.config import SUPPORTED_VIDEO_EXTS


def discover_raw_videos(raw_dir: Path) -> list[Path]:
    if not raw_dir.exists():
        return []

    videos: list[Path] = []
    for entry in raw_dir.iterdir():
        if entry.is_file() and entry.suffix.lower() in SUPPORTED_VIDEO_EXTS:
            videos.append(entry)

    return sorted(videos, key=lambda p: p.name.lower())
