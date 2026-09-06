"""Build the prompt pack: 6 questions x 3 prompting conditions = 18 prompts.

Reads questions/questions.json and the C3 rule sets in config/conditions.yaml, renders the
three prompting conditions and writes outputs/<run>/prompts.json, which is what the
collector submits to each assistant.

  C1 - zero-shot          the question on its own, with no role, context or instructions
  C2 - structured-expert  an expert persona plus prescribed answer headings
  C3 - rule-grounded      the C2 question augmented with explicit, topic-specific rules

Usage (RAC_RUN_DIR selects the run folder under outputs/, default "study"):
  python scripts/build_prompts.py
"""

from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
QUESTIONS = ROOT / "questions" / "questions.json"
CONDITIONS = ROOT / "config" / "conditions.yaml"
OUT = ROOT / "outputs" / os.environ.get("RAC_RUN_DIR", "study")


def c1(text: str) -> str:
    return text.strip()


def c2(text: str) -> str:
    return (
        "You are a senior concrete-materials and structures expert advising on recycled "
        "aggregate concrete (RAC). Answer precisely. Explicitly state your confidence, the "
        "boundary conditions under which your answer holds, and any open debates.\n\n"
        f"Question:\n{text.strip()}\n\n"
        "End with: (a) a one-line summary, (b) key assumptions, (c) what remains uncertain."
    )


def c3(text: str, rules: list[str]) -> str:
    rule_lines = "\n".join(f"- {r}" for r in rules)
    return f"Apply these established findings when answering:\n{rule_lines}\n\nNow answer:\n{text.strip()}"


def main() -> None:
    questions = json.loads(QUESTIONS.read_text(encoding="utf-8"))
    conditions = json.loads(CONDITIONS.read_text(encoding="utf-8"))
    c3_rules = next(c["rules"] for c in conditions["conditions"] if c["id"] == "C3")

    prompts = []
    for q in questions:
        rules = c3_rules.get(q["topic_id"])
        if not rules:
            raise SystemExit(f"No C3 rules defined for topic {q['topic_id']}")
        prompts.append({"question_id": q["question_id"], "condition": "C1", "prompt": c1(q["text"])})
        prompts.append({"question_id": q["question_id"], "condition": "C2", "prompt": c2(q["text"])})
        prompts.append({"question_id": q["question_id"], "condition": "C3", "prompt": c3(q["text"], rules)})

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "prompts.json").write_text(
        json.dumps(prompts, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {len(prompts)} prompts ({len(questions)} questions x 3 conditions) -> {OUT / 'prompts.json'}")


if __name__ == "__main__":
    main()
