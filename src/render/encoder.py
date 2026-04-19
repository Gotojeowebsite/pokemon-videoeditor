from __future__ import annotations

import json
import re
import subprocess
from collections import Counter
from pathlib import Path

from src.config import EditorConfig
from src.effects.rip_sequence import (
    build_audio_expression_for_windows,
    build_sad_music_volume_expression,
    can_use_sad_music,
)
from src.overlay.compositor import (
    build_overlay_assets,
    build_overlay_windows,
    build_video_overlay_filters,
)


try:
    import cv2  # type: ignore
except Exception:  # pragma: no cover
    cv2 = None


CROP_PATTERN = re.compile(r"crop=(\d+):(\d+):(\d+):(\d+)")


def probe_duration(video_path: Path, ffprobe_bin: str = "ffprobe") -> float:
    cmd = [
        ffprobe_bin,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "json",
        str(video_path),
    ]
    result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    payload = json.loads(result.stdout)
    duration_text = payload.get("format", {}).get("duration", "0")
    return max(0.0, float(duration_text))


def has_audio_stream(video_path: Path, ffprobe_bin: str = "ffprobe") -> bool:
    cmd = [
        ffprobe_bin,
        "-v",
        "error",
        "-select_streams",
        "a",
        "-show_entries",
        "stream=codec_type",
        "-of",
        "json",
        str(video_path),
    ]
    result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    payload = json.loads(result.stdout)
    streams = payload.get("streams", [])
    return isinstance(streams, list) and len(streams) > 0


def probe_dimensions(video_path: Path, ffprobe_bin: str = "ffprobe") -> tuple[int, int]:
    cmd = [
        ffprobe_bin,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height",
        "-of",
        "json",
        str(video_path),
    ]
    result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    payload = json.loads(result.stdout)
    streams = payload.get("streams", [])
    if not streams or not isinstance(streams, list):
        return 0, 0

    stream0 = streams[0] if isinstance(streams[0], dict) else {}
    width = int(stream0.get("width", 0) or 0)
    height = int(stream0.get("height", 0) or 0)
    return width, height


def _normalize_even(value: int) -> int:
    return value if value % 2 == 0 else max(2, value - 1)


def _opencv_border_crop(
    video_path: Path,
    *,
    start_offset: float,
    probe_seconds: float,
) -> tuple[int, int, int, int] | None:
    if cv2 is None:
        return None

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        return None

    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    start_frame = max(0, int(start_offset * fps))
    cap.set(cv2.CAP_PROP_POS_FRAMES, start_frame)

    target_samples = 12
    step = max(1, int((max(1.0, probe_seconds) * fps) / target_samples))
    frame_idx = 0
    samples: list[tuple[int, int, int, int]] = []

    while len(samples) < target_samples:
        ok, frame = cap.read()
        if not ok:
            break

        if frame_idx % step != 0:
            frame_idx += 1
            continue

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        _, mask = cv2.threshold(gray, 18, 255, cv2.THRESH_BINARY)
        mask = cv2.medianBlur(mask, 5)
        coords = cv2.findNonZero(mask)
        if coords is not None:
            x, y, w, h = cv2.boundingRect(coords)
            samples.append((x, y, w, h))

        frame_idx += 1

    cap.release()
    if not samples:
        return None

    left = max(sample[0] for sample in samples)
    top = max(sample[1] for sample in samples)
    right = min(sample[0] + sample[2] for sample in samples)
    bottom = min(sample[1] + sample[3] for sample in samples)
    if right - left <= 2 or bottom - top <= 2:
        return None

    return right - left, bottom - top, left, top


def detect_source_crop(
    video_path: Path,
    *,
    ffmpeg_bin: str,
    source_width: int,
    source_height: int,
    start_offset: float,
    probe_seconds: float,
    min_gain_pixels: int,
) -> tuple[int, int, int, int] | None:
    if source_width <= 0 or source_height <= 0:
        return None

    cmd = [
        ffmpeg_bin,
        "-v",
        "info",
        "-ss",
        f"{max(0.0, start_offset):.3f}",
        "-t",
        f"{max(1.0, probe_seconds):.3f}",
        "-i",
        str(video_path),
        "-vf",
        "cropdetect=24:16:0",
        "-f",
        "null",
        "-",
    ]
    result = subprocess.run(cmd, check=False, capture_output=True, text=True)
    crop_w = crop_h = crop_x = crop_y = 0
    crop_lines = CROP_PATTERN.findall((result.stderr or "") + "\n" + (result.stdout or ""))
    if crop_lines:
        counts = Counter(crop_lines)
        best, _ = max(
            counts.items(),
            key=lambda item: (
                item[1],
                int(item[0][0]) * int(item[0][1]),
            ),
        )
        crop_w, crop_h, crop_x, crop_y = (int(best[0]), int(best[1]), int(best[2]), int(best[3]))
    else:
        fallback_crop = _opencv_border_crop(
            video_path,
            start_offset=start_offset,
            probe_seconds=probe_seconds,
        )
        if fallback_crop is None:
            return None
        crop_w, crop_h, crop_x, crop_y = fallback_crop

    crop_w = _normalize_even(min(max(2, crop_w), source_width))
    crop_h = _normalize_even(min(max(2, crop_h), source_height))
    crop_x = max(0, min(source_width - 2, crop_x))
    crop_y = max(0, min(source_height - 2, crop_y))
    crop_x = crop_x if crop_x % 2 == 0 else crop_x - 1
    crop_y = crop_y if crop_y % 2 == 0 else crop_y - 1

    if crop_x + crop_w > source_width:
        crop_w = _normalize_even(source_width - crop_x)
    if crop_y + crop_h > source_height:
        crop_h = _normalize_even(source_height - crop_y)

    gain_w = source_width - crop_w
    gain_h = source_height - crop_h
    if gain_w < min_gain_pixels and gain_h < min_gain_pixels:
        return None

    return crop_w, crop_h, crop_x, crop_y


def _shift_events_for_trim(events: list[dict], trim_start: float) -> list[dict]:
    shifted: list[dict] = []
    for event in events:
        ts = float(event.get("timestamp", 0.0)) - trim_start
        if ts < 0.0:
            continue
        cloned = dict(event)
        cloned["timestamp"] = ts
        shifted.append(cloned)
    return shifted


def render_video(
    config: EditorConfig,
    input_video: Path,
    output_video: Path,
    initial_state: dict,
    timeline_events: list[dict],
    death_events: list[dict],
) -> None:
    full_duration = probe_duration(input_video, ffprobe_bin=config.ffprobe_bin)
    intro_trim = max(0.0, min(config.intro_trim_seconds, max(0.0, full_duration - 0.1)))
    duration = max(0.1, full_duration - intro_trim)

    render_fps = int(config.output_fps)
    render_preset = "medium"
    use_blurred_background = True

    if full_duration >= 5400.0:
        render_fps = min(render_fps, 30)
        render_preset = "veryfast"
        use_blurred_background = False
        print(
            "Using adaptive render profile "
            f"({render_fps}fps, preset {render_preset}, non-blurred gameplay background)."
        )
    elif full_duration >= 3600.0:
        render_fps = min(render_fps, 48)
        render_preset = "fast"
        print(f"Using adaptive render profile ({render_fps}fps, preset {render_preset}).")

    shifted_timeline_events = _shift_events_for_trim(timeline_events, intro_trim)
    shifted_death_events = _shift_events_for_trim(death_events, intro_trim)

    source_width, source_height = probe_dimensions(input_video, ffprobe_bin=config.ffprobe_bin)
    source_crop: tuple[int, int, int, int] | None = None
    if config.auto_cropdetect_enabled:
        source_crop = detect_source_crop(
            input_video,
            ffmpeg_bin=config.ffmpeg_bin,
            source_width=source_width,
            source_height=source_height,
            start_offset=intro_trim + config.auto_cropdetect_start_offset,
            probe_seconds=config.auto_cropdetect_probe_seconds,
            min_gain_pixels=config.auto_crop_min_gain_pixels,
        )

    windows = build_overlay_windows(initial_state, shifted_timeline_events, duration)
    overlay_assets = build_overlay_assets(config=config, windows=windows, video_stem=input_video.stem)
    video_chain = build_video_overlay_filters(
        windows=overlay_assets,
        death_events=shifted_death_events,
        output_width=config.output_width,
        output_height=config.output_height,
        output_fps=render_fps,
        gameplay_x=config.gameplay_x,
        gameplay_y=config.gameplay_y,
        gameplay_width=config.gameplay_width,
        gameplay_height=config.gameplay_height,
        source_trim_start=intro_trim,
        source_crop=source_crop,
        preserve_full_frame=config.gameplay_preserve_full_frame,
        use_blurred_background=use_blurred_background,
    )

    input_has_audio = has_audio_stream(input_video, ffprobe_bin=config.ffprobe_bin)

    duck_expr = build_audio_expression_for_windows(shifted_death_events)
    if input_has_audio:
        audio_chain_parts = [
            (
                f"[0:a]atrim=start={intro_trim:.3f},asetpts=PTS-STARTPTS,"
                f"aformat=fltp:44100:stereo,volume='{duck_expr}'[basea]"
            )
        ]
    else:
        audio_chain_parts = [
            f"anullsrc=r=44100:cl=stereo,atrim=0:{duration:.3f},asetpts=PTS-STARTPTS,volume='{duck_expr}'[basea]"
        ]

    cmd = [
        config.ffmpeg_bin,
        "-y",
        "-i",
        str(input_video),
    ]

    if can_use_sad_music(config.sad_music_path):
        sad_expr = build_sad_music_volume_expression(shifted_death_events)
        cmd.extend(["-stream_loop", "-1", "-i", str(config.sad_music_path)])
        audio_chain_parts.append(
            f"[1:a]atrim=0:{duration:.3f},asetpts=PTS-STARTPTS,volume='{sad_expr}'[sada]"
        )
        audio_chain_parts.append("[basea][sada]amix=inputs=2:duration=first:dropout_transition=0[aout]")
    else:
        audio_chain_parts.append("[basea]anull[aout]")

    filter_complex = video_chain + ";" + ";".join(audio_chain_parts)

    cmd.extend(
        [
            "-filter_complex",
            filter_complex,
            "-map",
            "[vout]",
            "-map",
            "[aout]",
            "-t",
            f"{duration:.3f}",
            "-c:v",
            "libx264",
            "-preset",
            render_preset,
            "-b:v",
            config.video_bitrate,
            "-r",
            str(render_fps),
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            str(output_video),
        ]
    )

    output_video.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(cmd, check=True)
