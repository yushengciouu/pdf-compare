from pathlib import Path
from difflib import SequenceMatcher

import cv2
import numpy as np


def _extract_signature(image_path: Path, thumb_size: int) -> np.ndarray:
    image = cv2.imread(str(image_path), cv2.IMREAD_GRAYSCALE)
    if image is None:
        raise RuntimeError(f"無法讀取頁面影像: {image_path}")
    thumb = cv2.resize(image, (thumb_size, thumb_size), interpolation=cv2.INTER_AREA)
    norm = thumb.astype(np.float32) / 255.0
    return norm.reshape(-1)


def _similarity(a: np.ndarray, b: np.ndarray) -> float:
    mad = float(np.mean(np.abs(a - b)))
    return max(0.0, 1.0 - mad)


def _text_similarity(a: str, b: str) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return float(SequenceMatcher(a=a.lower(), b=b.lower()).ratio())


def _build_signatures(
    render_dir: Path, pages: int, thumb_size: int
) -> list[np.ndarray]:
    return [
        _extract_signature(render_dir / f"{i:04d}.png", thumb_size)
        for i in range(1, pages + 1)
    ]


def _traceback(
    score: np.ndarray,
    trace: np.ndarray,
    sims: np.ndarray,
    min_similarity: float,
) -> list[tuple[str, int | None, int | None]]:
    i = score.shape[0] - 1
    j = score.shape[1] - 1
    actions: list[tuple[str, int | None, int | None]] = []

    while i > 0 or j > 0:
        move = int(trace[i, j])
        if move == 0:
            sim = float(sims[i - 1, j - 1])
            if sim >= min_similarity:
                actions.append(("paired", i, j))
            else:
                actions.append(("inserted", None, j))
                actions.append(("deleted", i, None))
            i -= 1
            j -= 1
        elif move == 1:
            actions.append(("deleted", i, None))
            i -= 1
        elif move == 2:
            actions.append(("inserted", None, j))
            j -= 1
        else:
            if i > 0:
                actions.append(("deleted", i, None))
                i -= 1
            elif j > 0:
                actions.append(("inserted", None, j))
                j -= 1

    actions.reverse()
    return actions


def build_smart_page_map(
    before_render_dir: Path,
    after_render_dir: Path,
    pages_before: int,
    pages_after: int,
    thumb_size: int,
    gap_penalty: float,
    match_bias: float,
    min_similarity: float,
    before_texts: list[str] | None = None,
    after_texts: list[str] | None = None,
    image_weight: float = 0.7,
    text_weight: float = 0.3,
) -> list[dict]:
    if pages_before == 0 and pages_after == 0:
        return []

    sig_before = _build_signatures(before_render_dir, pages_before, thumb_size)
    sig_after = _build_signatures(after_render_dir, pages_after, thumb_size)

    sims = np.zeros((pages_before, pages_after), dtype=np.float32)
    for i in range(pages_before):
        for j in range(pages_after):
            image_sim = _similarity(sig_before[i], sig_after[j])
            if before_texts is not None and after_texts is not None:
                text_sim = _text_similarity(before_texts[i], after_texts[j])
                sims[i, j] = float(
                    max(
                        0.0,
                        min(1.0, image_weight * image_sim + text_weight * text_sim),
                    )
                )
            else:
                sims[i, j] = image_sim

    score = np.full((pages_before + 1, pages_after + 1), -1e9, dtype=np.float32)
    trace = np.full((pages_before + 1, pages_after + 1), -1, dtype=np.int8)
    score[0, 0] = 0.0

    for i in range(1, pages_before + 1):
        score[i, 0] = score[i - 1, 0] - gap_penalty
        trace[i, 0] = 1
    for j in range(1, pages_after + 1):
        score[0, j] = score[0, j - 1] - gap_penalty
        trace[0, j] = 2

    for i in range(1, pages_before + 1):
        for j in range(1, pages_after + 1):
            match_score = score[i - 1, j - 1] + float(sims[i - 1, j - 1]) - match_bias
            delete_score = score[i - 1, j] - gap_penalty
            insert_score = score[i, j - 1] - gap_penalty

            best = match_score
            move = 0
            if delete_score > best:
                best = delete_score
                move = 1
            if insert_score > best:
                best = insert_score
                move = 2

            score[i, j] = best
            trace[i, j] = move

    actions = _traceback(score, trace, sims, min_similarity)

    page_map: list[dict] = []
    for slot, (state, before_page, after_page) in enumerate(actions, start=1):
        page_map.append(
            {
                "slot": slot,
                "before_page": before_page,
                "after_page": after_page,
                "state": state,
            }
        )
    return page_map
