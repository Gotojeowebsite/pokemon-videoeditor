from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path


try:
    import cv2  # type: ignore
    from PIL import Image
    import imagehash
except Exception:  # pragma: no cover
    cv2 = None
    Image = None
    imagehash = None


@dataclass
class Fingerprint:
    sha256: str
    perceptual: str


def file_sha256(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def sampled_perceptual_hash(path: Path, sample_count: int = 3) -> str:
    if cv2 is None or Image is None or imagehash is None:
        return ""

    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        return ""

    frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    if frame_count <= 0:
        cap.release()
        return ""

    indices = [max(0, int((frame_count - 1) * (i + 1) / (sample_count + 1))) for i in range(sample_count)]
    hashes: list[str] = []

    for idx in indices:
        cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
        ok, frame = cap.read()
        if not ok:
            continue
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        pil_image = Image.fromarray(rgb)
        hashes.append(str(imagehash.phash(pil_image)))

    cap.release()
    return "-".join(hashes)


def build_fingerprint(path: Path) -> Fingerprint:
    return Fingerprint(
        sha256=file_sha256(path),
        perceptual=sampled_perceptual_hash(path),
    )


def similarity_score(a: Fingerprint, b: Fingerprint) -> float:
    if a.sha256 and b.sha256 and a.sha256 == b.sha256:
        return 1.0

    if not a.perceptual or not b.perceptual:
        return 0.0

    if a.perceptual == b.perceptual:
        return 0.95

    a_parts = a.perceptual.split("-")
    b_parts = b.perceptual.split("-")
    if len(a_parts) != len(b_parts) or not a_parts:
        return 0.0

    equal_parts = sum(1 for x, y in zip(a_parts, b_parts) if x == y)
    return equal_parts / len(a_parts)
