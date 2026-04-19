from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil


@dataclass
class EditorConfig:
    root_dir: Path
    raw_dir: Path
    events_dir: Path
    output_dir: Path
    processed_log_path: Path
    team_state_path: Path
    sad_music_path: Path
    flower_overlay_path: Path
    template_base_path: Path
    icons_dir: Path
    dead_pokemon_log_path: Path
    state_timeline_log_path: Path
    overlay_runtime_dir: Path
    ffmpeg_bin: str = "ffmpeg"
    ffprobe_bin: str = "ffprobe"
    duplicate_threshold: float = 0.9
    output_width: int = 1920
    output_height: int = 1080
    output_fps: int = 60
    video_bitrate: str = "8M"
    ocr_interval_seconds: float = 2.0
    max_graveyard_icons: int = 18
    gameplay_x: int = 490
    gameplay_y: int = 78
    gameplay_width: int = 1138
    gameplay_height: int = 806
    gameplay_preserve_full_frame: bool = True
    intro_trim_seconds: float = 2.5
    auto_cropdetect_enabled: bool = True
    auto_cropdetect_probe_seconds: float = 18.0
    auto_cropdetect_start_offset: float = 3.0
    auto_crop_min_gain_pixels: int = 80
    enable_location_web_lookup: bool = True
    location_web_lookup_timeout_seconds: float = 2.5
    location_consensus_hits: int = 2
    location_consensus_window_seconds: float = 12.0


SUPPORTED_VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm"}


def _resolve_binary(name: str, root_dir: Path) -> str:
    found = shutil.which(name)
    if found:
        return found

    local_appdata = Path.home() / "AppData" / "Local"
    winget_candidate = (
        local_appdata
        / "Microsoft"
        / "WinGet"
        / "Packages"
        / "Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe"
        / "ffmpeg-8.1-full_build"
        / "bin"
        / f"{name}.exe"
    )
    if winget_candidate.exists():
        return str(winget_candidate)

    project_tool_candidate = root_dir / "tools" / "ffmpeg" / "bin" / f"{name}.exe"
    if project_tool_candidate.exists():
        return str(project_tool_candidate)

    return name


def load_config(root_dir: Path) -> EditorConfig:
    data_dir = root_dir / "data"
    assets_dir = root_dir / "assets"
    ffmpeg_bin = _resolve_binary("ffmpeg", root_dir)
    ffprobe_bin = _resolve_binary("ffprobe", root_dir)

    return EditorConfig(
        root_dir=root_dir,
        raw_dir=data_dir / "raw_footage",
        events_dir=data_dir / "events",
        output_dir=data_dir / "final_cuts",
        processed_log_path=data_dir / "processed_log.json",
        team_state_path=data_dir / "team_state.json",
        sad_music_path=assets_dir / "music" / "sad_theme.mp3",
        flower_overlay_path=assets_dir / "template" / "flowers.png",
        template_base_path=assets_dir / "template" / "base.png",
        icons_dir=assets_dir / "icons",
        dead_pokemon_log_path=data_dir / "dead_pokemon_log.json",
        state_timeline_log_path=data_dir / "state_timeline_log.json",
        overlay_runtime_dir=data_dir / "_runtime" / "overlay_frames",
        ffmpeg_bin=ffmpeg_bin,
        ffprobe_bin=ffprobe_bin,
    )
