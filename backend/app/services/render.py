from pathlib import Path

import fitz


def get_page_count(pdf_path: Path) -> int:
    with fitz.open(pdf_path) as doc:
        return doc.page_count


def render_pdf_pages(pdf_path: Path, output_dir: Path, dpi: int) -> int:
    output_dir.mkdir(parents=True, exist_ok=True)
    zoom = dpi / 72.0
    matrix = fitz.Matrix(zoom, zoom)

    with fitz.open(pdf_path) as doc:
        for index, page in enumerate(doc, start=1):
            pix = page.get_pixmap(matrix=matrix, alpha=False)
            out = output_dir / f"{index:04d}.png"
            pix.save(out)
        return doc.page_count
