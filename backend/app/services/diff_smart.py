from pathlib import Path

from app.core.config import Settings
from app.services.page_match import build_smart_page_map


def plan_smart_mapping(
    settings: Settings,
    before_render_dir: Path,
    after_render_dir: Path,
    pages_before: int,
    pages_after: int,
    before_texts: list[str] | None = None,
    after_texts: list[str] | None = None,
) -> list[dict]:
    return build_smart_page_map(
        before_render_dir=before_render_dir,
        after_render_dir=after_render_dir,
        pages_before=pages_before,
        pages_after=pages_after,
        thumb_size=settings.smart_thumb_size,
        gap_penalty=settings.smart_gap_penalty,
        match_bias=settings.smart_match_bias,
        min_similarity=settings.smart_min_similarity,
        before_texts=before_texts,
        after_texts=after_texts,
        image_weight=settings.smart_image_weight,
        text_weight=settings.smart_text_weight,
    )
