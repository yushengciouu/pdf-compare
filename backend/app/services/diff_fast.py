import json
from pathlib import Path

import cv2
import numpy as np


def _as_gray(image: np.ndarray) -> np.ndarray:
    if len(image.shape) == 2:
        return image
    return cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)


def _resize_to_common(
    img_a: np.ndarray, img_b: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    h = min(img_a.shape[0], img_b.shape[0])
    w = min(img_a.shape[1], img_b.shape[1])
    return (
        cv2.resize(img_a, (w, h), interpolation=cv2.INTER_AREA),
        cv2.resize(img_b, (w, h), interpolation=cv2.INTER_AREA),
    )


def compare_images(
    before_png: Path, after_png: Path, mask_out: Path, boxes_out: Path
) -> tuple[int, int, int]:
    img_a = cv2.imread(str(before_png), cv2.IMREAD_COLOR)
    img_b = cv2.imread(str(after_png), cv2.IMREAD_COLOR)
    if img_a is None or img_b is None:
        raise RuntimeError("無法讀取渲染影像")

    img_a, img_b = _resize_to_common(img_a, img_b)
    gray_a = cv2.GaussianBlur(_as_gray(img_a), (3, 3), 0)
    gray_b = cv2.GaussianBlur(_as_gray(img_b), (3, 3), 0)

    threshold = 25
    signed = gray_a.astype(np.int16) - gray_b.astype(np.int16)
    # signed > 0 代表 before 較亮、after 較暗，通常是 after 新增深色內容
    after_added_raw = (signed > threshold).astype(np.uint8) * 255
    # signed < 0 代表 before 較暗、after 較亮，通常是 after 移除原內容
    after_removed_raw = (signed < -threshold).astype(np.uint8) * 255

    kernel = np.ones((3, 3), np.uint8)
    after_added_clean = cv2.morphologyEx(after_added_raw, cv2.MORPH_OPEN, kernel)
    after_added_clean = cv2.morphologyEx(after_added_clean, cv2.MORPH_CLOSE, kernel)
    after_removed_clean = cv2.morphologyEx(after_removed_raw, cv2.MORPH_OPEN, kernel)
    after_removed_clean = cv2.morphologyEx(after_removed_clean, cv2.MORPH_CLOSE, kernel)

    union = cv2.bitwise_or(after_added_clean, after_removed_clean)

    num_labels, labels, stats, _centroids = cv2.connectedComponentsWithStats(
        union, connectivity=8
    )

    boxes: list[dict] = []
    for i in range(1, num_labels):
        x, y, w, h, area = stats[i]
        if int(area) < 40:
            continue

        component = labels == i
        after_added_count = int(np.count_nonzero(after_added_clean[component]))
        after_removed_count = int(np.count_nonzero(after_removed_clean[component]))
        if after_removed_count > after_added_count:
            change_type = "removed_in_after"
        elif after_added_count > after_removed_count:
            change_type = "added_in_after"
        else:
            change_type = "content_change"

        boxes.append(
            {
                "x": int(x),
                "y": int(y),
                "w": int(w),
                "h": int(h),
                "score": float(min(1.0, area / 5000.0)),
                "type": change_type,
            }
        )

    rgba = np.zeros((union.shape[0], union.shape[1], 4), dtype=np.uint8)
    after_added_pixels = after_added_clean > 0
    after_removed_pixels = after_removed_clean > 0
    overlap_pixels = after_added_pixels & after_removed_pixels

    # BGRA: 紅色 = 左有右無、青色 = 右有左無
    rgba[after_removed_pixels] = (0, 0, 255, 220)
    rgba[after_added_pixels] = (255, 255, 0, 220)
    rgba[overlap_pixels] = (255, 255, 255, 220)
    rgba[:, :, 3] = np.where(union > 0, 220, 0).astype(np.uint8)

    mask_out.parent.mkdir(parents=True, exist_ok=True)
    boxes_out.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(mask_out), rgba)
    with boxes_out.open("w", encoding="utf-8") as f:
        json.dump(boxes, f, ensure_ascii=False, indent=2)

    return len(boxes), int(union.shape[1]), int(union.shape[0])
