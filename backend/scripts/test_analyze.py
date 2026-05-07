"""
用 testfile/ 目錄下的兩份 PDF 直接執行 LLM 分析，印出結果。
"""
from __future__ import annotations
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import get_settings
from app.services.llm_analyze import build_analyze_report

TESTFILE_DIR = PROJECT_ROOT.parent / "testfile"
BEFORE = TESTFILE_DIR / "L022-0496-15.pdf"
AFTER  = TESTFILE_DIR / "L022-0496-17 (1).pdf"

settings = get_settings()
print(f"before: {BEFORE}")
print(f"after : {AFTER}")
print("正在分析，請稍候...\n")

result = build_analyze_report(BEFORE, AFTER, settings)

print(f"overall_summary: {result.get('overall_summary')}")
print(f"summary: {result.get('summary')}")
print()
for p in result.get("pages", []):
    before_page = p.get("before_page", "N/A")
    after_page  = p.get("after_page",  "N/A")
    print(f"Slot {p['slot']:3d} | {p.get('state','?'):8s} | before:{before_page} → after:{after_page} | [{p.get('importance','?')}]")
    print(f"  summary: {p.get('summary','')}")
    for c in p.get("changes", []):
        print(f"  [{c.get('type')}] {c.get('description','')}")
    print()
