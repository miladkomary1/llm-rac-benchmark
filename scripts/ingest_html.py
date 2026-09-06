"""Extract plain text and metadata from the saved answer HTML files.

Every response saved by the collector is a self-contained HTML page carrying a
<script type="application/json" id="rac-data">{...}</script> block with the answer text,
the answer HTML, the references and the metadata (assistant, question, condition,
repetition, word count). This reads every *.html in a folder and writes captured.jsonl in
the run directory, which is the input of the repeatability analysis.

Usage:
  RAC_RUN_DIR=study python scripts/ingest_html.py outputs/study/rac_html
Then:
  RAC_RUN_DIR=study python scripts/analyse_repeatability.py
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "outputs" / os.environ.get("RAC_RUN_DIR", "study")
RAC_DATA = re.compile(
    r'<script[^>]*id=["\']rac-data["\'][^>]*>(.*?)</script>', re.S | re.I)


def parse_html(path: Path) -> dict | None:
    html = path.read_text(encoding="utf-8", errors="replace")
    m = RAC_DATA.search(html)
    if not m:
        print(f"  ! {path.name}: no <script id='rac-data'> block")
        return None
    try:
        d = json.loads(m.group(1).strip())
    except json.JSONDecodeError as e:
        print(f"  ! {path.name}: bad JSON in rac-data ({e})")
        return None
    text = (d.get("answer_text") or "").strip()
    if not d.get("response_id") or not text:
        print(f"  ! {path.name}: missing response_id or answer_text")
        return None
    return {
        "response_id": d["response_id"],
        "tool": d.get("tool", ""),
        "model_note": d.get("model_note", ""),
        "collected_date": d.get("collected_at", "") or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "prompt_url": "",
        "mistake": False,
        "mistake_reason": "",
        "text": text,
        "references": d.get("references", []) or [],
        "word_count": d.get("word_count") or len(text.split()),
    }


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit("usage: python scripts/ingest_html.py <folder_of_html>  (RAC_RUN_DIR selects output run)")
    folder = Path(sys.argv[1])
    if not folder.is_dir():
        sys.exit(f"not a folder: {folder}")
    files = sorted(folder.glob("*.html"))
    if not files:
        sys.exit(f"no .html files in {folder}")

    BASE.mkdir(parents=True, exist_ok=True)
    out = BASE / "captured.jsonl"
    # keep-last dedup by response_id (re-ingest is safe)
    rows: dict[str, dict] = {}
    if out.exists():
        for line in out.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                rows[r["response_id"]] = r

    added = 0
    for f in files:
        rec = parse_html(f)
        if rec:
            rows[rec["response_id"]] = rec
            added += 1

    with out.open("w", encoding="utf-8") as fh:
        for rid in sorted(rows):
            fh.write(json.dumps(rows[rid], ensure_ascii=False) + "\n")

    tools = {}
    for r in rows.values():
        tools[r["tool"]] = tools.get(r["tool"], 0) + 1
    print(f"ingested {added} html files -> {out}  (total unique: {len(rows)})  by tool: {tools}")


if __name__ == "__main__":
    main()
