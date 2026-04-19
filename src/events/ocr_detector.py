from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote_plus
from urllib.request import Request, urlopen


try:
    import cv2  # type: ignore
    import pytesseract  # type: ignore
except Exception:  # pragma: no cover
    cv2 = None
    pytesseract = None


OCR_KEYWORDS = ("fainted", "fainted!", "has fainted")
CATCH_KEYWORDS = ("was caught", "gotcha", "you caught")
LOCATION_PATTERNS = (
    r"\broute\s+\d+\b",
    r"\b[a-z]+\s+(?:city|town|cave|forest|road|tower|lake|island|park|harbor|harbour|bay)\b",
    r"\b(?:mt|mount)\.?\s+[a-z]+\b",
    r"\b(?:victory\s+road|indigo\s+plateau|pokemon\s+league|pokemon\s+center)\b",
)

KNOWN_LOCATION_NAMES = (
    "pallet town",
    "viridian city",
    "pewter city",
    "cerulean city",
    "vermilion city",
    "lavender town",
    "celadon city",
    "fuchsia city",
    "saffron city",
    "cinnabar island",
    "indigo plateau",
    "mt moon",
    "rock tunnel",
    "diglett cave",
    "victory road",
    "new bark town",
    "cherrygrove city",
    "violet city",
    "azalea town",
    "goldenrod city",
    "ecruteak city",
    "olivine city",
    "blackthorn city",
    "sprout tower",
    "union cave",
    "ilex forest",
    "lake of rage",
)

LOCATION_HINT_TERMS = (
    "route",
    "city",
    "town",
    "cave",
    "forest",
    "road",
    "tower",
    "lake",
    "island",
    "mt",
    "mount",
    "league",
    "center",
)

GOOGLE_SUGGEST_ENDPOINT = "https://suggestqueries.google.com/complete/search"
_GOOGLE_LOOKUP_CACHE: dict[str, str] = {}
_GOOGLE_LOOKUP_DISABLED = False
_GOOGLE_LOOKUP_ATTEMPTS = 0
_MAX_GOOGLE_LOOKUPS_PER_RUN = 8

WINDOWS_TESSERACT_CANDIDATES = (
    Path("C:/Program Files/Tesseract-OCR/tesseract.exe"),
    Path("C:/Program Files (x86)/Tesseract-OCR/tesseract.exe"),
    Path("C:/Users/Public/Tesseract-OCR/tesseract.exe"),
)

DEFAULT_LOCATION_CONSENSUS_HITS = 2
DEFAULT_LOCATION_CONSENSUS_WINDOW_SECONDS = 12.0
LOCATION_FUZZY_MATCH_THRESHOLD = 0.62


@dataclass(frozen=True)
class LocationGuess:
    location: str
    confidence: float
    reasoning: str
    source: str


def _ensure_tesseract_binary() -> bool:
    if pytesseract is None:
        return False

    try:
        _ = pytesseract.get_tesseract_version()
        return True
    except Exception:
        pass

    for candidate in WINDOWS_TESSERACT_CANDIDATES:
        if not candidate.exists():
            continue
        try:
            pytesseract.pytesseract.tesseract_cmd = str(candidate)
            _ = pytesseract.get_tesseract_version()
            return True
        except Exception:
            continue

    return False


def _normalize_ocr_text(text: str) -> str:
    text = text.replace("\n", " ").replace("\r", " ")
    text = re.sub(r"[^a-zA-Z0-9\s\-']+", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _location_norm_key(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9\s]+", " ", str(value).strip().lower())
    normalized = normalized.replace("mount ", "mt ")
    normalized = normalized.replace("harbour", "harbor")
    return re.sub(r"\s+", " ", normalized).strip()


def _best_known_location(candidate: str) -> tuple[str, float]:
    candidate_key = _location_norm_key(candidate)
    if not candidate_key:
        return "", 0.0

    best_name = ""
    best_score = 0.0
    candidate_tokens = set(candidate_key.split())

    for known in KNOWN_LOCATION_NAMES:
        known_key = _location_norm_key(known)
        if not known_key:
            continue

        ratio = difflib.SequenceMatcher(None, candidate_key, known_key).ratio()
        known_tokens = set(known_key.split())
        overlap = len(candidate_tokens & known_tokens)
        token_cover = overlap / max(1, len(known_tokens))
        score = max(ratio, (ratio + token_cover) / 2)

        if candidate_key == known_key:
            return _titlecase_location(known), 1.0
        if candidate_key in known_key and len(candidate_key) >= max(5, int(len(known_key) * 0.6)):
            score = max(score, 0.84)
        if known_key in candidate_key and len(known_key) >= 5:
            score = max(score, 0.82)

        if score > best_score:
            best_score = score
            best_name = _titlecase_location(known)

    return best_name, best_score


def _normalize_location_candidate(
    candidate: str,
    *,
    location_corrections: dict[str, str] | None,
) -> tuple[str, float, str]:
    candidate = _titlecase_location(candidate)
    key = _location_norm_key(candidate)
    if not key:
        return "", 0.0, ""

    if location_corrections:
        corrected = location_corrections.get(key)
        if corrected:
            normalized = _titlecase_location(corrected)
            return normalized, 0.24, f"Sidecar correction mapped '{candidate}' to '{normalized}'."

    best_name, best_score = _best_known_location(candidate)
    if best_name and best_score >= LOCATION_FUZZY_MATCH_THRESHOLD:
        if best_score >= 0.80:
            bonus = 0.22
        elif best_score >= 0.72:
            bonus = 0.16
        else:
            bonus = 0.10
        return best_name, bonus, f"Fuzzy-matched OCR location '{candidate}' to known '{best_name}' ({best_score:.2f})."

    return candidate, 0.0, "Used raw OCR location text (no strong normalization match)."


def _titlecase_location(location: str) -> str:
    words: list[str] = []
    for raw in location.strip().split():
        word = raw.lower().strip(" .,")
        if word in {"mt", "mt."}:
            words.append("Mt")
        elif word == "pokemon":
            words.append("Pokemon")
        elif word.isdigit():
            words.append(word)
        else:
            words.append(word.capitalize())
    return " ".join(words)


def _extract_name_from_text(text: str) -> str:
    match = re.search(r"([a-zA-Z0-9\-']{2,})\s+(?:has\s+)?fainted", text)
    if not match:
        return ""
    candidate = match.group(1).strip(" -_.,!?:;")
    return candidate.title()


def _extract_caught_name(text: str) -> str:
    match = re.search(r"([a-zA-Z0-9\-']{2,})\s+was\s+caught", text)
    if not match:
        return ""
    return match.group(1).strip(" -_.,!?:;").title()


def _extract_location_candidates(text: str) -> list[str]:
    normalized = _normalize_ocr_text(text.lower())
    if not normalized:
        return []

    found: list[str] = []
    for pattern in LOCATION_PATTERNS:
        for match in re.finditer(pattern, normalized):
            found.append(_titlecase_location(match.group(0)))

    for known in KNOWN_LOCATION_NAMES:
        if known in normalized:
            found.append(_titlecase_location(known))

    deduped: list[str] = []
    seen: set[str] = set()
    for candidate in found:
        key = candidate.strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(candidate)
    return deduped


def _score_location_candidate(candidate: str, region: str) -> float:
    text = candidate.lower()
    score = 0.40
    if re.search(r"\broute\s+\d+\b", text):
        score += 0.32
    if any(text.endswith(suffix) for suffix in ("city", "town", "cave", "forest", "road", "tower", "lake", "island")):
        score += 0.20
    if any(term in text for term in ("mt", "league", "center", "park", "bay")):
        score += 0.08
    if region == "location_banner":
        score += 0.22
    elif region == "top":
        score += 0.14
    elif region == "full":
        score += 0.05
    elif region == "battle_log":
        score -= 0.10
    elif region == "bottom":
        score -= 0.04
    return max(0.0, min(0.98, score))


def _build_ocr_regions(frame) -> list[tuple[str, object, str]]:
    h, w = frame.shape[:2]
    top = frame[0 : max(1, int(h * 0.28)), :]
    bottom = frame[max(0, int(h * 0.58)) : h, :]
    center = frame[max(0, int(h * 0.18)) : max(1, int(h * 0.90)), max(0, int(w * 0.06)) : max(1, int(w * 0.94))]
    location_banner = frame[max(0, int(h * 0.04)) : max(1, int(h * 0.21)), max(0, int(w * 0.15)) : max(1, int(w * 0.86))]
    battle_log = frame[max(0, int(h * 0.63)) : h, max(0, int(w * 0.04)) : max(1, int(w * 0.96))]
    return [
        ("full", frame, "--psm 6"),
        ("location_banner", location_banner, "--psm 7"),
        ("top", top, "--psm 7"),
        ("bottom", bottom, "--psm 6"),
        ("battle_log", battle_log, "--psm 6"),
        ("center", center, "--psm 6"),
    ]


def _prepare_region_for_ocr(region):
    gray = cv2.cvtColor(region, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (3, 3), 0)
    gray = cv2.equalizeHist(gray)
    return cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]


def _read_region_text(region, psm_config: str) -> str:
    prepared = _prepare_region_for_ocr(region)
    for image in (prepared, cv2.bitwise_not(prepared)):
        try:
            text = pytesseract.image_to_string(image, config=f"--oem 3 {psm_config}")
        except Exception:
            return ""
        cleaned = text.strip()
        if cleaned:
            return cleaned
    return ""


def _extract_location_hint(text: str) -> str:
    normalized = _normalize_ocr_text(text.lower())
    if not normalized:
        return ""
    if any(keyword in normalized for keyword in OCR_KEYWORDS + CATCH_KEYWORDS):
        return ""
    if not any(term in normalized for term in LOCATION_HINT_TERMS):
        return ""
    if "route" in normalized and not re.search(r"route\s+\d+", normalized):
        return ""
    if len(normalized.split()) < 2:
        return ""
    return normalized[:48]


def _apply_pokemon_correction(name: str, pokemon_corrections: dict[str, str] | None) -> str:
    candidate = str(name).strip()
    if not candidate:
        return ""

    if not pokemon_corrections:
        return candidate

    lower = candidate.lower()
    squashed = re.sub(r"[^a-z0-9]+", "", lower)
    for raw_key, value in pokemon_corrections.items():
        key = str(raw_key).strip().lower()
        if not key:
            continue
        key_squashed = re.sub(r"[^a-z0-9]+", "", key)
        if lower == key or squashed == key_squashed:
            return str(value).strip() or candidate

    return candidate


def _google_location_lookup(hint_text: str, timeout_seconds: float) -> LocationGuess | None:
    global _GOOGLE_LOOKUP_DISABLED
    global _GOOGLE_LOOKUP_ATTEMPTS

    if _GOOGLE_LOOKUP_DISABLED or _GOOGLE_LOOKUP_ATTEMPTS >= _MAX_GOOGLE_LOOKUPS_PER_RUN:
        return None

    key = hint_text.strip().lower()
    if not key:
        return None

    key_tokens = re.findall(r"[a-z0-9]+", key)
    if len(key_tokens) > 4:
        key = " ".join(key_tokens[:4])

    if key in _GOOGLE_LOOKUP_CACHE:
        cached = _GOOGLE_LOOKUP_CACHE[key]
        if not cached:
            return None
        return LocationGuess(
            location=cached,
            confidence=0.66,
            reasoning=f"Google suggestion cache confirmed location hint '{hint_text}'.",
            source="ocr+web",
        )

    query = quote_plus(f"pokemon {hint_text} location")
    url = f"{GOOGLE_SUGGEST_ENDPOINT}?client=firefox&q={query}"
    request = Request(url, headers={"User-Agent": "Mozilla/5.0"})

    suggestions: list[str] = []
    _GOOGLE_LOOKUP_ATTEMPTS += 1
    try:
        with urlopen(request, timeout=max(0.2, timeout_seconds)) as response:
            payload = json.loads(response.read().decode("utf-8", errors="ignore"))
        if isinstance(payload, list) and len(payload) > 1 and isinstance(payload[1], list):
            suggestions = [str(item) for item in payload[1]]
    except Exception:
        _GOOGLE_LOOKUP_DISABLED = True
        _GOOGLE_LOOKUP_CACHE[key] = ""
        return None

    for suggestion in suggestions:
        candidates = _extract_location_candidates(suggestion)
        if candidates:
            location = candidates[0]
            _GOOGLE_LOOKUP_CACHE[key] = location
            return LocationGuess(
                location=location,
                confidence=0.66,
                reasoning=f"Google suggestion matched hint '{hint_text}' to '{location}'.",
                source="ocr+web",
            )

    _GOOGLE_LOOKUP_CACHE[key] = ""
    return None


def _infer_location_from_regions(
    region_texts: dict[str, str],
    *,
    enable_web_lookup: bool,
    web_lookup_timeout_seconds: float,
    location_corrections: dict[str, str] | None,
) -> LocationGuess | None:
    local_candidates: list[LocationGuess] = []

    for region_name, text in region_texts.items():
        normalized = _normalize_ocr_text(text.lower())
        if not normalized:
            continue
        for candidate in _extract_location_candidates(normalized):
            normalized_candidate, bonus, normalization_reason = _normalize_location_candidate(
                candidate,
                location_corrections=location_corrections,
            )
            if not normalized_candidate:
                continue
            confidence = min(0.99, _score_location_candidate(normalized_candidate, region_name) + bonus)
            local_candidates.append(
                LocationGuess(
                    location=normalized_candidate,
                    confidence=confidence,
                    reasoning=(
                        f"OCR {region_name} region matched location pattern. "
                        f"{normalization_reason}"
                    ).strip(),
                    source="ocr",
                )
            )

    best_local: LocationGuess | None = None
    if local_candidates:
        local_candidates.sort(key=lambda item: item.confidence, reverse=True)
        best_local = local_candidates[0]
        if best_local.confidence >= 0.62:
            return best_local

    if enable_web_lookup:
        hint = _extract_location_hint(region_texts.get("location_banner", ""))
        if not hint:
            hint = _extract_location_hint(region_texts.get("top", ""))
        if not hint:
            hint = _extract_location_hint(region_texts.get("full", ""))
        if hint:
            web_guess = _google_location_lookup(hint, web_lookup_timeout_seconds)
            if web_guess and (best_local is None or web_guess.confidence >= best_local.confidence + 0.05):
                normalized_location, bonus, normalization_reason = _normalize_location_candidate(
                    web_guess.location,
                    location_corrections=location_corrections,
                )
                return LocationGuess(
                    location=normalized_location or web_guess.location,
                    confidence=min(0.99, web_guess.confidence + bonus),
                    reasoning=(
                        f"{web_guess.reasoning} {normalization_reason}"
                    ).strip(),
                    source=web_guess.source,
                )

    if best_local and best_local.confidence >= 0.45:
        return best_local
    return None


def _event_confidence(event: dict) -> float:
    try:
        return float(event.get("confidence", 0.0))
    except (TypeError, ValueError):
        return 0.0


def _dedupe_events_by_type(events: list[dict]) -> list[dict]:
    if not events:
        return []

    events = sorted(events, key=lambda e: e.get("timestamp", 0.0))
    compacted: list[dict] = []

    for event in events:
        event_type = str(event.get("type", ""))
        ts = float(event.get("timestamp", 0.0))
        matched = False
        for existing in compacted:
            same_type = str(existing.get("type", "")) == event_type
            existing_name = str(existing.get("pokemon", "")).strip().lower()
            incoming_name = str(event.get("pokemon", "")).strip().lower()
            same_name = existing_name == incoming_name and bool(existing_name)
            same_location = str(existing.get("location", "")).strip().lower() == str(event.get("location", "")).strip().lower()
            if not same_type:
                continue

            # tighter windows for location updates, wider for text spam on battle/catches
            window = 15.0 if event_type == "location" else 8.0
            if abs(float(existing.get("timestamp", 0.0)) - ts) > window:
                continue

            if event_type == "location" and same_location:
                if _event_confidence(event) > _event_confidence(existing):
                    existing.update(event)
                matched = True
                break
            if event_type in {"death", "catch"} and (same_name or (not existing_name and not incoming_name)):
                if incoming_name and not existing_name:
                    existing["pokemon"] = event.get("pokemon", "")
                if _event_confidence(event) > _event_confidence(existing):
                    existing.update(event)
                matched = True
                break

        if not matched:
            compacted.append(event)

    return compacted


def detect_auto_events_ocr(
    video_path: Path,
    sample_every_seconds: float = 2.0,
    *,
    duration_hint_seconds: float | None = None,
    enable_web_lookup: bool = True,
    web_lookup_timeout_seconds: float = 2.5,
    location_corrections: dict[str, str] | None = None,
    pokemon_corrections: dict[str, str] | None = None,
    location_consensus_hits: int = DEFAULT_LOCATION_CONSENSUS_HITS,
    location_consensus_window_seconds: float = DEFAULT_LOCATION_CONSENSUS_WINDOW_SECONDS,
) -> list[dict]:
    if cv2 is None or pytesseract is None:
        return []

    if not _ensure_tesseract_binary():
        # Keep the pipeline running even when OCR binary is unavailable.
        return []

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        return []

    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    frame_step = max(1, int(round(fps * max(0.25, float(sample_every_seconds)))))
    fast_long_mode = float(sample_every_seconds) >= 6.0

    detected: list[dict] = []
    location_hit_windows: dict[str, list[float]] = {}
    consensus_hits = max(1, int(location_consensus_hits))
    consensus_window_seconds = max(sample_every_seconds, float(location_consensus_window_seconds))
    last_emitted_location_key = ""
    last_emitted_location_ts = -9999.0

    def process_sampled_frame(frame, sampled_frame_idx: int) -> None:
        nonlocal last_emitted_location_key
        nonlocal last_emitted_location_ts

        region_defs = {name: (img, psm) for name, img, psm in _build_ocr_regions(frame)}
        region_texts: dict[str, str] = {}

        def read_region(region_name: str) -> str:
            if region_name in region_texts:
                return region_texts[region_name]
            payload = region_defs.get(region_name)
            if payload is None:
                return ""
            region_img, psm = payload
            text = _read_region_text(region_img, psm)
            if text:
                region_texts[region_name] = text
                return text
            return ""

        preferred_regions = ("location_banner", "battle_log") if fast_long_mode else (
            "location_banner",
            "battle_log",
            "full",
            "top",
        )
        for preferred_region in preferred_regions:
            read_region(preferred_region)

        merged_text = _normalize_ocr_text(" ".join(region_texts.values()).lower())
        if not merged_text and not fast_long_mode:
            read_region("center")
            read_region("bottom")
            merged_text = _normalize_ocr_text(" ".join(region_texts.values()).lower())

        if merged_text:
            battle_text = _normalize_ocr_text(
                " ".join(
                    region_texts.get(name, "")
                    for name in ("battle_log", "bottom", "center", "full")
                ).lower()
            )

            if not fast_long_mode and not any(keyword in battle_text for keyword in OCR_KEYWORDS + CATCH_KEYWORDS):
                read_region("bottom")
                battle_text = _normalize_ocr_text(
                    " ".join(
                        region_texts.get(name, "")
                        for name in ("battle_log", "bottom", "center", "full")
                    ).lower()
                )

            location_guess = _infer_location_from_regions(
                region_texts,
                enable_web_lookup=enable_web_lookup,
                web_lookup_timeout_seconds=web_lookup_timeout_seconds,
                location_corrections=location_corrections,
            )

            if location_guess is None and not fast_long_mode:
                read_region("center")
                location_guess = _infer_location_from_regions(
                    region_texts,
                    enable_web_lookup=enable_web_lookup,
                    web_lookup_timeout_seconds=web_lookup_timeout_seconds,
                    location_corrections=location_corrections,
                )

            timestamp = sampled_frame_idx / fps

            if any(keyword in battle_text for keyword in OCR_KEYWORDS):
                death_name = _apply_pokemon_correction(
                    _extract_name_from_text(battle_text),
                    pokemon_corrections,
                )
                detected.append(
                    {
                        "timestamp": timestamp,
                        "type": "death",
                        "pokemon": death_name,
                        "location": "",
                        "source": "ocr",
                        "confidence": 0.80,
                        "reasoning": "Detected fainted text in OCR output.",
                    }
                )

            if any(keyword in battle_text for keyword in CATCH_KEYWORDS):
                catch_name = _apply_pokemon_correction(
                    _extract_caught_name(battle_text),
                    pokemon_corrections,
                )
                catch_location = location_guess.location if location_guess else ""
                if catch_name or catch_location:
                    detected.append(
                        {
                            "timestamp": timestamp,
                            "type": "catch",
                            "pokemon": catch_name,
                            "location": catch_location,
                            "source": "ocr",
                            "confidence": max(0.60, location_guess.confidence if location_guess else 0.60),
                            "reasoning": (
                                location_guess.reasoning if location_guess else "Detected catch text in OCR output."
                            ),
                        }
                    )

            if location_guess:
                location_key = _location_norm_key(location_guess.location)
                if location_key:
                    window = location_hit_windows.setdefault(location_key, [])
                    window.append(timestamp)
                    window[:] = [t for t in window if (timestamp - t) <= consensus_window_seconds]
                    hits = len(window)
                    consensus_passed = hits >= consensus_hits or location_guess.confidence >= 0.84

                    if consensus_passed:
                        same_recent = (
                            location_key == last_emitted_location_key
                            and (timestamp - last_emitted_location_ts) < (consensus_window_seconds * 0.9)
                        )
                        if not same_recent:
                            boosted_confidence = min(0.99, location_guess.confidence + min(0.16, 0.04 * max(0, hits - 1)))
                            detected.append(
                                {
                                    "timestamp": timestamp,
                                    "type": "location",
                                    "pokemon": "",
                                    "location": location_guess.location,
                                    "source": location_guess.source,
                                    "confidence": boosted_confidence,
                                    "reasoning": (
                                        f"{location_guess.reasoning} "
                                        f"Consensus {hits}/{consensus_hits} sightings in {consensus_window_seconds:.0f}s."
                                    ).strip(),
                                }
                            )
                            last_emitted_location_key = location_key
                            last_emitted_location_ts = timestamp

    if fast_long_mode:
        duration_seconds = max(0.0, float(duration_hint_seconds or 0.0))
        frame_count = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0.0
        if duration_seconds <= 0.0 and fps > 0.0 and frame_count > 0.0:
            duration_seconds = frame_count / fps

        seek_time = 0.0
        step_seconds = max(0.5, float(sample_every_seconds))
        max_samples = 0
        if duration_seconds > 0.0:
            max_samples = int(duration_seconds / step_seconds) + 5
        elif frame_count > 0.0 and frame_step > 0:
            max_samples = int(frame_count / frame_step) + 5
        else:
            max_samples = 20000

        sample_counter = 0
        last_sampled_idx = -1
        stagnant_reads = 0

        while True:
            if sample_counter >= max_samples:
                break
            if duration_seconds > 0.0 and seek_time > duration_seconds:
                break

            cap.set(cv2.CAP_PROP_POS_MSEC, seek_time * 1000.0)
            ok, frame = cap.read()
            if not ok:
                break

            sampled_idx = int(max(0.0, cap.get(cv2.CAP_PROP_POS_FRAMES) - 1.0))
            if sampled_idx <= last_sampled_idx:
                stagnant_reads += 1
            else:
                stagnant_reads = 0
            if stagnant_reads >= 3:
                break

            process_sampled_frame(frame, sampled_idx)
            sample_counter += 1
            last_sampled_idx = sampled_idx
            seek_time += step_seconds
    else:
        frame_idx = 0
        reached_end = False
        while True:
            ok, frame = cap.read()
            if not ok:
                break

            process_sampled_frame(frame, frame_idx)

            # Advance quickly through non-sampled frames without decoding each one.
            frame_idx += 1
            skipped = 0
            while skipped < frame_step - 1:
                if not cap.grab():
                    reached_end = True
                    break
                frame_idx += 1
                skipped += 1

            if reached_end:
                break

    cap.release()
    return _dedupe_events_by_type(detected)


def detect_death_events_ocr(video_path: Path, sample_every_seconds: float = 2.0) -> list[dict]:
    auto_events = detect_auto_events_ocr(video_path, sample_every_seconds)
    deaths = [event for event in auto_events if event.get("type") == "death"]
    return _dedupe_nearby_events(deaths)


def _dedupe_nearby_events(events: list[dict], window_seconds: float = 8.0) -> list[dict]:
    if not events:
        return []

    events = sorted(events, key=lambda e: e["timestamp"])
    compacted: list[dict] = [events[0]]

    for event in events[1:]:
        prev = compacted[-1]
        if event["timestamp"] - prev["timestamp"] <= window_seconds:
            continue
        compacted.append(event)

    return compacted
