from __future__ import annotations

import difflib
import io
import json
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen

from PIL import Image, ImageDraw, ImageFont

from src.config import EditorConfig


@dataclass
class OverlayWindow:
    start: float
    end: float
    state: dict


@dataclass
class OverlayAssetWindow:
    start: float
    end: float
    overlay_path: Path


LEFT_PANEL = (12, 112, 468, 776)
RIGHT_TOP = (1640, 112, 268, 86)
RIGHT_CATCHES = (1640, 215, 268, 74)
RIGHT_LOCATION = (1640, 392, 268, 74)
RIGHT_GRAVEYARD = (1640, 492, 268, 172)
RIGHT_PC_BOX = (1640, 680, 268, 192)
BOTTOM_LEFT = (12, 888, 600, 102)
BOTTOM_RIGHT = (1442, 900, 466, 90)

ICON_EXTENSIONS = {".png", ".webp", ".jpg", ".jpeg"}
ICON_INDEX_CACHE: dict[str, dict[str, Path]] = {}
ICON_IMAGE_CACHE: dict[str, Image.Image] = {}
ICON_FETCH_ATTEMPTED: set[str] = set()
POKEAPI_NAME_INDEX: list[str] | None = None
POKEAPI_NAME_INDEX_FAILED = False
POKEAPI_RESOLVE_CACHE: dict[str, str] = {}

POKEMON_ICON_ALIASES = {
    "unsparce": "dunsparce",
    "astly": "gastly",
    "mr_mime": "mr-mime",
    "mime_jr": "mime-jr",
    "nidoran_f": "nidoran-f",
    "nidoran_m": "nidoran-m",
    "ho_oh": "ho-oh",
    "porygon_z": "porygon-z",
}


def _load_pokeapi_name_index() -> list[str]:
    global POKEAPI_NAME_INDEX
    global POKEAPI_NAME_INDEX_FAILED

    if POKEAPI_NAME_INDEX is not None:
        return POKEAPI_NAME_INDEX
    if POKEAPI_NAME_INDEX_FAILED:
        return []

    request = Request(
        "https://pokeapi.co/api/v2/pokemon?limit=2000",
        headers={"User-Agent": "Mozilla/5.0"},
    )
    try:
        with urlopen(request, timeout=3.0) as response:
            payload = json.loads(response.read().decode("utf-8", errors="ignore"))
    except Exception:
        POKEAPI_NAME_INDEX_FAILED = True
        return []

    results = payload.get("results", [])
    names: list[str] = []
    if isinstance(results, list):
        for item in results:
            if not isinstance(item, dict):
                continue
            raw_name = str(item.get("name", "")).strip().lower()
            if raw_name:
                names.append(raw_name)

    POKEAPI_NAME_INDEX = sorted(set(names))
    return POKEAPI_NAME_INDEX


def _fuzzy_match_api_name(raw_candidate: str) -> str:
    candidate = raw_candidate.strip().lower()
    if not candidate:
        return ""

    cached = POKEAPI_RESOLVE_CACHE.get(candidate)
    if cached:
        return cached

    index = _load_pokeapi_name_index()
    if not index:
        return ""

    direct = difflib.get_close_matches(candidate, index, n=1, cutoff=0.80)
    if direct:
        match = direct[0]
        POKEAPI_RESOLVE_CACHE[candidate] = match
        return match

    compact = candidate.replace("-", "")
    best_name = ""
    best_score = 0.0
    for name in index:
        score = difflib.SequenceMatcher(None, compact, name.replace("-", "")).ratio()
        if score > best_score:
            best_score = score
            best_name = name

    if best_name and best_score >= 0.86:
        POKEAPI_RESOLVE_CACHE[candidate] = best_name
        return best_name

    return ""


def build_overlay_windows(initial_state: dict, timeline_events: list[dict], duration: float) -> list[OverlayWindow]:
    events = sorted(timeline_events, key=lambda e: e.get("timestamp", 0.0))
    windows: list[OverlayWindow] = []

    current_state = initial_state
    start = 0.0

    for event in events:
        ts = float(event.get("timestamp", 0.0))
        if ts > start:
            windows.append(OverlayWindow(start=start, end=min(ts, duration), state=current_state))

        snapshot = event.get("team_state_snapshot")
        if isinstance(snapshot, dict):
            current_state = snapshot
        start = ts

    if start < duration:
        windows.append(OverlayWindow(start=start, end=duration, state=current_state))

    if not windows:
        windows.append(OverlayWindow(start=0.0, end=duration, state=initial_state))

    return windows


def _sanitize_name(name: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "_", name.strip().lower())
    return cleaned.strip("_")


def _icon_index(icons_dir: Path) -> dict[str, Path]:
    cache_key = str(icons_dir.resolve())
    cached = ICON_INDEX_CACHE.get(cache_key)
    if cached is not None:
        return cached

    index: dict[str, Path] = {}
    if icons_dir.exists():
        for entry in icons_dir.iterdir():
            if not entry.is_file() or entry.suffix.lower() not in ICON_EXTENSIONS:
                continue
            index[_sanitize_name(entry.stem)] = entry

    ICON_INDEX_CACHE[cache_key] = index
    return index


def _invalidate_icon_index(icons_dir: Path) -> None:
    cache_key = str(icons_dir.resolve())
    ICON_INDEX_CACHE.pop(cache_key, None)


def _candidate_api_names(pokemon_name: str) -> list[str]:
    key = _sanitize_name(pokemon_name)
    if not key:
        return []

    candidates: list[str] = []

    cached = POKEAPI_RESOLVE_CACHE.get(key)
    if cached:
        candidates.append(cached)

    alias = POKEMON_ICON_ALIASES.get(key)
    if alias:
        candidates.append(alias)

    dashed = key.replace("_", "-")
    candidates.append(dashed)

    # Some OCR names miss a leading letter (e.g., astly -> gastly).
    if len(dashed) >= 4 and dashed[0].isalpha():
        candidates.append(f"g{dashed}")

    fuzzy = _fuzzy_match_api_name(dashed)
    if fuzzy:
        candidates.append(fuzzy)

    deduped: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        norm = candidate.strip().lower()
        if not norm or norm in seen:
            continue
        seen.add(norm)
        deduped.append(norm)
    return deduped


def _extract_sprite_url(payload: dict) -> str:
    sprites = payload.get("sprites", {})
    if not isinstance(sprites, dict):
        return ""

    other = sprites.get("other", {})
    if isinstance(other, dict):
        official = other.get("official-artwork", {})
        if isinstance(official, dict):
            candidate = str(official.get("front_default", "")).strip()
            if candidate:
                return candidate

        home = other.get("home", {})
        if isinstance(home, dict):
            candidate = str(home.get("front_default", "")).strip()
            if candidate:
                return candidate

    candidate = str(sprites.get("front_default", "")).strip()
    if candidate:
        return candidate
    return ""


def _download_icon_for_name(config: EditorConfig, pokemon_name: str, target_key: str) -> Path | None:
    if not target_key:
        return None

    config.icons_dir.mkdir(parents=True, exist_ok=True)
    save_path = config.icons_dir / f"{target_key}.png"

    headers = {"User-Agent": "Mozilla/5.0"}
    for api_name in _candidate_api_names(pokemon_name):
        try:
            api_req = Request(
                f"https://pokeapi.co/api/v2/pokemon/{quote(api_name)}",
                headers=headers,
            )
            with urlopen(api_req, timeout=2.5) as response:
                payload = json.loads(response.read().decode("utf-8", errors="ignore"))
            sprite_url = _extract_sprite_url(payload)
            if not sprite_url:
                continue

            sprite_req = Request(sprite_url, headers=headers)
            with urlopen(sprite_req, timeout=2.5) as response:
                image_bytes = response.read()
            if not image_bytes:
                continue

            icon = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
            icon.save(save_path, "PNG")
            POKEAPI_RESOLVE_CACHE[target_key] = api_name
            _invalidate_icon_index(config.icons_dir)
            return save_path
        except Exception:
            continue

    return None


def _find_icon_path(config: EditorConfig, pokemon_name: str) -> Path | None:
    index = _icon_index(config.icons_dir)
    key = _sanitize_name(pokemon_name)

    if not index and key and key not in ICON_FETCH_ATTEMPTED:
        ICON_FETCH_ATTEMPTED.add(key)
        downloaded = _download_icon_for_name(config, pokemon_name, key)
        if downloaded is not None:
            return downloaded
        index = _icon_index(config.icons_dir)

    if not index:
        return None

    if key in index:
        return index[key]

    compact_key = key.replace("_", "")
    for candidate_key, candidate_path in index.items():
        if candidate_key.replace("_", "") == compact_key:
            return candidate_path

    fuzzy_matches = difflib.get_close_matches(key, list(index.keys()), n=1, cutoff=0.72)
    if fuzzy_matches:
        return index[fuzzy_matches[0]]

    if key and key not in ICON_FETCH_ATTEMPTED:
        ICON_FETCH_ATTEMPTED.add(key)
        downloaded = _download_icon_for_name(config, pokemon_name, key)
        if downloaded is not None:
            return downloaded

    return None


def prefetch_pokemon_icons(config: EditorConfig, pokemon_names: list[str]) -> dict[str, object]:
    unique_names: list[str] = []
    seen: set[str] = set()
    for raw_name in pokemon_names:
        name = str(raw_name).strip()
        if not name:
            continue
        lowered = name.lower()
        if lowered in {"unknown", "???", "none", "null", "-"}:
            continue
        if lowered in seen:
            continue
        seen.add(lowered)
        unique_names.append(name)

    resolved = 0
    missing: list[str] = []
    for name in unique_names:
        if _find_icon_path(config, name) is not None:
            resolved += 1
        else:
            missing.append(name)

    return {
        "requested": len(unique_names),
        "resolved": resolved,
        "missing": missing,
    }


def _load_icon(config: EditorConfig, pokemon_name: str, icon_size: int) -> Image.Image:
    icon_path = _find_icon_path(config, pokemon_name)
    if icon_path is not None and icon_path.exists():
        cache_key = f"{str(icon_path.resolve())}|{icon_size}"
        cached = ICON_IMAGE_CACHE.get(cache_key)
        if cached is not None:
            return cached.copy()
        icon = Image.open(icon_path).convert("RGBA")
        resized = icon.resize((icon_size, icon_size), Image.Resampling.LANCZOS)
        ICON_IMAGE_CACHE[cache_key] = resized
        return resized.copy()

    fallback = Image.new("RGBA", (icon_size, icon_size), (20, 26, 60, 230))
    draw = ImageDraw.Draw(fallback)
    draw.rectangle((0, 0, icon_size - 1, icon_size - 1), outline=(255, 230, 0, 255), width=2)
    initials = (pokemon_name[:2] or "??").upper()
    font = ImageFont.load_default()
    text_w = draw.textlength(initials, font=font)
    draw.text(((icon_size - text_w) / 2, icon_size / 2 - 6), initials, fill=(255, 255, 255, 255), font=font)
    return fallback


def _draw_team_panel(canvas: Image.Image, draw: ImageDraw.ImageDraw, config: EditorConfig, team: list[str]) -> None:
    x, y, w, h = LEFT_PANEL
    panel_top = y + 24
    panel_bottom = y + h - 8
    available_height = max(120, panel_bottom - panel_top)
    slot_h = max(48, int(available_height / 6))
    icon_size = max(28, min(72, slot_h - 18))
    font = ImageFont.load_default()

    for idx in range(6):
        slot_y = panel_top + idx * slot_h
        line_y = min(slot_y + slot_h, panel_bottom)
        draw.line((x + 10, line_y, x + w - 10, line_y), fill=(32, 70, 150, 190), width=1)

        name = team[idx] if idx < len(team) else ""
        icon_x = x + 16
        icon_y = slot_y + max(2, int((slot_h - icon_size) / 2))

        if name:
            icon = _load_icon(config, name, icon_size)
            label = _truncate_text(name, 20)
        else:
            icon = Image.new("RGBA", (icon_size, icon_size), (9, 16, 40, 220))
            icon_draw = ImageDraw.Draw(icon)
            icon_draw.rectangle((0, 0, icon_size - 1, icon_size - 1), outline=(60, 100, 175, 220), width=2)
            label = "-"

        canvas.alpha_composite(icon, (icon_x, icon_y))
        text_x = icon_x + icon_size + 12
        text_y = icon_y + max(0, int(icon_size / 2) - 6)
        draw.text((text_x, text_y), f"{idx + 1}. {label}", fill=(255, 255, 255, 255), font=font)


def _draw_panel(draw: ImageDraw.ImageDraw, rect: tuple[int, int, int, int], title: str) -> None:
    x, y, w, h = rect
    draw.rectangle((x, y, x + w, y + h), outline=(255, 229, 0, 255), width=3, fill=(5, 8, 36, 190))
    draw.text((x + 8, y + 6), title, fill=(0, 245, 255, 255), font=ImageFont.load_default())


def _truncate_text(value: str, max_chars: int) -> str:
    text = str(value or "").strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3].rstrip() + "..."


def _ensure_state_shape(state: dict) -> dict:
    normalized = dict(state)
    normalized["active_team"] = [str(x).strip() for x in normalized.get("active_team", []) if str(x).strip()]
    normalized["graveyard"] = [str(x).strip() for x in normalized.get("graveyard", []) if str(x).strip()]
    normalized["pc_box"] = [str(x).strip() for x in normalized.get("pc_box", []) if str(x).strip()]
    normalized["location"] = str(normalized.get("location", "Unknown")).strip() or "Unknown"
    normalized["location_confidence"] = float(normalized.get("location_confidence", 0.0))
    normalized["location_reasoning"] = str(normalized.get("location_reasoning", "")).strip()
    normalized["catches"] = int(normalized.get("catches", 0))
    normalized["master"] = str(normalized.get("master", "J")).strip() or "J"
    normalized["event_count"] = int(normalized.get("event_count", 0))
    normalized["last_event"] = str(normalized.get("last_event", "No timeline events yet.")).strip() or "No timeline events yet."
    normalized["last_event_source"] = str(normalized.get("last_event_source", "none")).strip() or "none"
    return normalized


def _render_overlay_frame(config: EditorConfig, state: dict, out_path: Path) -> None:
    state = _ensure_state_shape(state)
    canvas = Image.new("RGBA", (config.output_width, config.output_height), (0, 0, 0, 0))

    if config.template_base_path.exists():
        base = Image.open(config.template_base_path).convert("RGBA")
        base = base.resize((config.output_width, config.output_height), Image.Resampling.LANCZOS)
        canvas.alpha_composite(base, (0, 0))

    draw = ImageDraw.Draw(canvas)
    font = ImageFont.load_default()

    _draw_panel(draw, LEFT_PANEL, "MY TEAM")
    _draw_panel(draw, RIGHT_TOP, "POKEMON MASTER")
    _draw_panel(draw, RIGHT_CATCHES, "CATCHES")
    _draw_panel(draw, RIGHT_LOCATION, "LOCATION")
    _draw_panel(draw, RIGHT_GRAVEYARD, "GRAVEYARD")
    _draw_panel(draw, RIGHT_PC_BOX, "PC BOX")
    _draw_panel(draw, BOTTOM_LEFT, "LIVE LOG")
    _draw_panel(draw, BOTTOM_RIGHT, "NUZLOCKE")

    team = state["active_team"][:6]
    _draw_team_panel(canvas, draw, config, team)

    draw.text((RIGHT_TOP[0] + 12, RIGHT_TOP[1] + 34), state["master"], fill=(255, 240, 0, 255), font=font)
    draw.text((RIGHT_CATCHES[0] + 12, RIGHT_CATCHES[1] + 34), str(state["catches"]), fill=(255, 255, 255, 255), font=font)
    draw.text((RIGHT_LOCATION[0] + 12, RIGHT_LOCATION[1] + 34), state["location"], fill=(255, 255, 255, 255), font=font)
    draw.text(
        (RIGHT_LOCATION[0] + 12, RIGHT_LOCATION[1] + 54),
        f"Conf: {state['location_confidence']:.2f}",
        fill=(180, 255, 220, 255),
        font=font,
    )

    draw.text(
        (BOTTOM_LEFT[0] + 12, BOTTOM_LEFT[1] + 34),
        f"Events: {state['event_count']} ({str(state['last_event_source']).upper()})",
        fill=(255, 255, 255, 255),
        font=font,
    )
    draw.text(
        (BOTTOM_LEFT[0] + 12, BOTTOM_LEFT[1] + 56),
        _truncate_text(state["last_event"], 90),
        fill=(255, 230, 140, 255),
        font=font,
    )

    fallen = state["graveyard"][: config.max_graveyard_icons]
    draw.text(
        (BOTTOM_RIGHT[0] + 12, BOTTOM_RIGHT[1] + 34),
        f"Fallen: {len(state['graveyard'])}",
        fill=(255, 104, 160, 255),
        font=font,
    )

    icon_size = 36
    cols = 6
    gap = 6
    start_x = RIGHT_GRAVEYARD[0] + 10
    start_y = RIGHT_GRAVEYARD[1] + 34
    for idx, pokemon_name in enumerate(fallen):
        row = idx // cols
        col = idx % cols
        x = start_x + col * (icon_size + gap)
        y = start_y + row * (icon_size + gap)
        icon = _load_icon(config, pokemon_name, icon_size)
        canvas.alpha_composite(icon, (x, y))

    pc_box_items = state["pc_box"]
    preview = ", ".join(pc_box_items[:5]) if pc_box_items else "-"
    if len(pc_box_items) > 5:
        preview = preview + f" (+{len(pc_box_items) - 5})"
    draw.text((RIGHT_PC_BOX[0] + 12, RIGHT_PC_BOX[1] + 34), preview, fill=(255, 255, 255, 255), font=font)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_path, "PNG")


def _ffmpeg_escape_path(path: Path) -> str:
    return str(path).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


def build_overlay_assets(
    config: EditorConfig,
    windows: list[OverlayWindow],
    video_stem: str,
) -> list[OverlayAssetWindow]:
    overlay_windows: list[OverlayAssetWindow] = []
    config.overlay_runtime_dir.mkdir(parents=True, exist_ok=True)
    for old in config.overlay_runtime_dir.glob(f"{video_stem}.overlay.*.png"):
        old.unlink(missing_ok=True)

    for idx, window in enumerate(windows):
        overlay_path = config.overlay_runtime_dir / f"{video_stem}.overlay.{idx:04d}.png"
        _render_overlay_frame(config, window.state, overlay_path)
        overlay_windows.append(
            OverlayAssetWindow(
                start=window.start,
                end=window.end,
                overlay_path=overlay_path,
            )
        )
    return overlay_windows


def build_team_text(state: dict) -> str:
    team = state.get("active_team", [])
    graveyard = state.get("graveyard", [])
    catches = state.get("catches", 0)
    location = state.get("location", "Unknown")

    team_text = "Team: " + ", ".join(team or ["-"])
    graveyard_text = "Fallen: " + ", ".join(graveyard or ["-"])
    catches_text = f"Catches: {catches}"
    location_text = f"Location: {location or 'Unknown'}"

    return f"{team_text} | {graveyard_text} | {catches_text} | {location_text}"


def build_video_overlay_filters(
    windows: list[OverlayAssetWindow],
    death_events: list[dict],
    output_width: int,
    output_height: int,
    output_fps: int,
    gameplay_x: int,
    gameplay_y: int,
    gameplay_width: int,
    gameplay_height: int,
    source_trim_start: float = 0.0,
    source_crop: tuple[int, int, int, int] | None = None,
    preserve_full_frame: bool = True,
    use_blurred_background: bool = True,
) -> str:
    chains: list[str] = [f"color=c=black:s={output_width}x{output_height}:r={output_fps}[bg]"]

    video_label = "0:v"
    if source_trim_start > 0.0:
        chains.append(f"[0:v]trim=start={source_trim_start:.3f},setpts=PTS-STARTPTS[vtrim]")
        video_label = "vtrim"

    if source_crop is not None:
        crop_w, crop_h, crop_x, crop_y = source_crop
        chains.append(f"[{video_label}]crop={crop_w}:{crop_h}:{crop_x}:{crop_y}[vcrop]")
        video_label = "vcrop"

    if preserve_full_frame:
        if use_blurred_background:
            gameplay_scale_chain = (
                f"[{video_label}]fps={output_fps},split=2[gamefgsrc][gamebgsrc];"
                f"[gamebgsrc]scale={gameplay_width}:{gameplay_height}:force_original_aspect_ratio=increase,"
                f"crop={gameplay_width}:{gameplay_height},boxblur=20:6,setsar=1[gamebg];"
                f"[gamefgsrc]scale={gameplay_width}:{gameplay_height}:force_original_aspect_ratio=decrease,"
                "setsar=1[gamefg];"
                "[gamebg][gamefg]overlay=(W-w)/2:(H-h)/2[game]"
            )
        else:
            gameplay_scale_chain = (
                f"[{video_label}]fps={output_fps},scale={gameplay_width}:{gameplay_height}:"
                "force_original_aspect_ratio=decrease,pad="
                f"{gameplay_width}:{gameplay_height}:(ow-iw)/2:(oh-ih)/2:color=black,"
                "setsar=1[game]"
            )
    else:
        gameplay_scale_chain = (
            f"[{video_label}]fps={output_fps},scale={gameplay_width}:{gameplay_height}:"
            "force_original_aspect_ratio=increase,crop="
            f"{gameplay_width}:{gameplay_height},"
            "setsar=1[game]"
        )

    chains.extend(
        [
            gameplay_scale_chain,
            f"[bg][game]overlay={gameplay_x}:{gameplay_y}[vbase]",
        ]
    )

    current_label = "vbase"
    for idx, window in enumerate(windows):
        movie_label = f"ov{idx}"
        out_label = f"v{idx + 1}"
        chains.append(f"movie='{_ffmpeg_escape_path(window.overlay_path)}',format=rgba[{movie_label}]")
        chains.append(
            f"[{current_label}][{movie_label}]overlay=0:0:enable='between(t,{window.start:.3f},{window.end:.3f})'[{out_label}]"
        )
        current_label = out_label

    draw_target = current_label

    for event in death_events:
        start = float(event.get("timestamp", 0.0))
        end = start + 4.0
        pokemon = str(event.get("pokemon") or "UNKNOWN").replace("'", "\\'")
        box_label = f"dbox_{int(start * 1000)}"
        text_label = f"dtext_{int(start * 1000)}"
        chains.append(
            f"[{draw_target}]drawbox=x=0:y=0:w=iw:h=ih:color=0x180010AA:t=fill:enable='between(t,{start:.3f},{end:.3f})'[{box_label}]"
        )
        chains.append(
            f"[{box_label}]drawtext=fontcolor=0xff66aa:fontsize=72:box=1:boxcolor=0x000000AA:boxborderw=18:x=(w-text_w)/2:y=(h-text_h)/2-40:text='RIP {pokemon}':enable='between(t,{start:.3f},{end:.3f})'[{text_label}]"
        )
        draw_target = text_label

    chains.append(f"[{draw_target}]format=yuv420p[vout]")
    return ";".join(chains)
