"""Run-to-run repeatability: how alike are the three repetitions of the same prompt?

For each (question x condition x assistant) group the captured answers are embedded with the
Voyage AI embedding API and compared pairwise by cosine similarity, reported as a percentage:
    sim_r1_r2, sim_r2_r3, sim_r1_r3, sim_mean   (0-100%)

A high percentage means the assistant produced near-identical answers across repetitions;
a low one means the substantive content drifted between runs. The word count of each answer
and the within-group word-count range are reported alongside, because stable meaning does not
imply stable length.

Requires a Voyage AI API key in the environment (never store it in the repository):
  PowerShell:  $env:VOYAGE_API_KEY = "..."
  bash:        export VOYAGE_API_KEY="..."
Keys are available at https://dashboard.voyageai.com/ .

Usage (RAC_RUN_DIR selects the run folder under outputs/, default "study"):
  RAC_RUN_DIR=study python scripts/analyse_repeatability.py

Output:
  outputs/<run>/eval/run_similarity.csv  one row per (question x condition x assistant) group
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.request
from itertools import combinations
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "outputs" / os.environ.get("RAC_RUN_DIR", "study")
CAPTURED = BASE / "captured.jsonl"
OUT = BASE / "eval"
VOYAGE_URL = "https://api.voyageai.com/v1/embeddings"
VOYAGE_MODEL = os.environ.get("VOYAGE_MODEL", "voyage-3.5")


def load_responses() -> list[dict]:
    rows = []
    for line in CAPTURED.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        rid = r["response_id"]
        parts = rid.split("_")  # <question_id>, <condition>, <assistant>, r<repeat>
        if len(parts) < 4:
            continue
        text = r.get("text", "")
        rows.append({"response_id": rid, "question_id": parts[0], "condition": parts[1],
                     "tool": parts[2], "repeat": parts[3].lstrip("r"), "text": text,
                     "words": len(text.split())})
    return rows


def _est_tokens(s: str) -> int:
    return max(1, len(s) // 4)  # ~4 chars/token, rough but safe for budgeting


def _batches(texts: list[str], max_tokens: int, max_items: int):
    """Group inputs so each request stays under the token/item caps (free tier = 10K TPM)."""
    batch, tok = [], 0
    for t in texts:
        tt = _est_tokens(t)
        if batch and (tok + tt > max_tokens or len(batch) >= max_items):
            yield batch
            batch, tok = [], 0
        batch.append(t)
        tok += tt
    if batch:
        yield batch


def voyage_embed(texts: list[str], api_key: str, model: str = VOYAGE_MODEL,
                 max_tokens: int = 8000, max_items: int = 16, min_interval: float = 0.0) -> list[list[float]]:
    """Embed a list of texts (token-budget batched, 429-aware). Returns vectors in input order.

    Free tier (no payment method) = 3 RPM / 10K TPM; on the first rate-limit hit we drop into a
    slow mode (one request / 60s) so the run still completes instead of crashing.
    """
    vecs: list[list[float]] = []
    batch_texts = [t[:30000] or " " for t in texts]
    batches = list(_batches(batch_texts, max_tokens, max_items))
    done = 0
    last = 0.0
    for bi, batch in enumerate(batches):
        wait = min_interval - (time.time() - last)
        if wait > 0:
            time.sleep(wait)
        body = json.dumps({"input": batch, "model": model}).encode("utf-8")
        for attempt in range(10):
            req = urllib.request.Request(VOYAGE_URL, data=body, method="POST", headers={
                "Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=120) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                rows = sorted(data["data"], key=lambda d: d["index"])
                vecs.extend(d["embedding"] for d in rows)
                last = time.time()
                break
            except urllib.error.HTTPError as e:
                msg = e.read().decode("utf-8", "ignore")
                if e.code == 429 and "payment method" in msg and min_interval < 60:
                    min_interval = 60.0  # free tier: throttle to ~1 req/min for the rest
                    print("  ! free-tier rate limit; switching to slow mode (~1 request/min)")
                if e.code in (429, 500, 502, 503) and attempt < 9:
                    time.sleep(65 if e.code == 429 else 2 ** attempt)  # clear the 1-min rolling window
                    continue
                raise SystemExit(f"Voyage API error {e.code}: {msg}")
        done += len(batch)
        print(f"  embedded {done}/{len(texts)} (batch {bi + 1}/{len(batches)})", flush=True)
    return vecs


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def pct(c: float) -> float:
    return round(max(0.0, min(1.0, c)) * 100, 1)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=VOYAGE_MODEL)
    ap.add_argument("--max-tokens", type=int, default=8000, help="token budget per request (free tier TPM=10K)")
    ap.add_argument("--max-items", type=int, default=16, help="max inputs per request")
    ap.add_argument("--min-interval", type=float, default=0.0,
                    help="min seconds between requests (free tier auto-uses 60 on first 429)")
    ap.add_argument("--tools", default=None, help="comma list to restrict to (e.g. chatgpt,claude)")
    ap.add_argument("--append", action="store_true", help="append rows to existing CSV (no header, keep by_id.json)")
    args = ap.parse_args()

    api_key = os.environ.get("VOYAGE_API_KEY")
    if not api_key:
        sys.exit("VOYAGE_API_KEY not set. PowerShell:  $env:VOYAGE_API_KEY = \"...\"\n"
                 "Keys: https://dashboard.voyageai.com/")

    rows = load_responses()
    if not rows:
        sys.exit(f"No responses in {CAPTURED}; run the collection first.")
    if args.tools:
        keep = set(args.tools.split(","))
        rows = [r for r in rows if r["tool"] in keep]
        if not rows:
            sys.exit(f"No responses for tools {args.tools}")

    # embed every answer once
    print(f"embedding {len(rows)} answers with Voyage ({args.model}) ...")
    vecs = voyage_embed([r["text"] for r in rows], api_key, args.model,
                        max_tokens=args.max_tokens, max_items=args.max_items,
                        min_interval=args.min_interval)
    vec_by_id = {r["response_id"]: v for r, v in zip(rows, vecs)}

    # group by question x condition x tool
    groups: dict[tuple, list[dict]] = {}
    for r in rows:
        groups.setdefault((r["question_id"], r["condition"], r["tool"]), []).append(r)

    OUT.mkdir(parents=True, exist_ok=True)
    csv_path = OUT / "run_similarity.csv"
    by_id: dict[str, dict] = {}
    fields = ["question_id", "condition", "tool", "n_runs",
              "sim_r1_r2", "sim_r2_r3", "sim_r1_r3", "sim_mean",
              "words_r1", "words_r2", "words_r3", "words_range", "model"]
    mode = "a" if (args.append and csv_path.exists()) else "w"
    with csv_path.open(mode, newline="", encoding="utf-8") as cf:
        w = csv.DictWriter(cf, fieldnames=fields)
        if mode == "w":
            w.writeheader()
        for (qid, cond, tool), members in sorted(groups.items()):
            members.sort(key=lambda m: m["repeat"])
            pair_pct: dict[str, float] = {}
            sims = []
            for a, b in combinations(members, 2):
                c = cosine(vec_by_id[a["response_id"]], vec_by_id[b["response_id"]])
                pair_pct[f"sim_r{a['repeat']}_r{b['repeat']}"] = pct(c)
                sims.append(c)
            mean = pct(sum(sims) / len(sims)) if sims else ""
            words_by_rep = {m["repeat"]: m["words"] for m in members}
            wc = [m["words"] for m in members]
            w.writerow({"question_id": qid, "condition": cond, "tool": tool,
                        "n_runs": len(members),
                        "sim_r1_r2": pair_pct.get("sim_r1_r2", ""),
                        "sim_r2_r3": pair_pct.get("sim_r2_r3", ""),
                        "sim_r1_r3": pair_pct.get("sim_r1_r3", ""),
                        "sim_mean": mean,
                        "words_r1": words_by_rep.get("1", ""), "words_r2": words_by_rep.get("2", ""),
                        "words_r3": words_by_rep.get("3", ""), "words_range": max(wc) - min(wc),
                        "model": args.model})

            for m in members:
                by_id[m["response_id"]] = {"sim_mean": mean, "group": f"{qid}_{cond}_{tool}"}
    if not args.append:
        (OUT / "run_similarity_by_id.json").write_text(
            json.dumps(by_id, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {len(groups)} groups to {csv_path.name} (mode={mode})")


if __name__ == "__main__":
    main()
