from __future__ import annotations

import argparse
import sys
from pathlib import Path

import fitz

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import get_settings
from app.services.storage import load_meta, new_job, read_json
from app.workers.tasks import run_compare_job


def make_pdf(path: Path, page_texts: list[str]) -> None:
    doc = fitz.open()
    for text in page_texts:
        page = doc.new_page()
        page.insert_text((72, 72), text)
    doc.save(path)
    doc.close()


def run_case(mode: str) -> None:
    settings = get_settings()
    job_id, job_root, _ = new_job(settings, mode)

    if mode == "fast":
        make_pdf(job_root / "input" / "before.pdf", ["Hello from before"])
        make_pdf(job_root / "input" / "after.pdf", ["Hello from after changed"])
    else:
        make_pdf(job_root / "input" / "before.pdf", ["P1 keep", "P2 old"])
        make_pdf(
            job_root / "input" / "after.pdf",
            ["P1 keep", "P2 inserted", "P3 old changed"],
        )

    run_compare_job(job_id, mode)

    meta = load_meta(settings, job_id)
    page_map = read_json(job_root / "page_map.json")

    print(f"job_id={job_id}")
    print(f"mode={mode}")
    print(f"status={meta.get('status')}")
    print(f"progress={meta.get('progress')}")
    print(f"stats={meta.get('stats')}")
    print(f"slots={len(page_map)}")
    print(f"job_root={job_root}")


def main() -> None:
    parser = argparse.ArgumentParser(description="PDF compare smoke test")
    parser.add_argument("--mode", choices=["fast", "smart"], default="fast")
    args = parser.parse_args()
    run_case(args.mode)


if __name__ == "__main__":
    main()
