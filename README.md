# llm-rac-benchmark

Collection toolchain for the study *"We taught LLMs, but can they teach us? Insights from
recycled aggregate concrete and beyond"*.

The study evaluates what the freely available web tiers of three conversational assistants —
ChatGPT (OpenAI), Gemini (Google) and Claude (Anthropic) — answer when asked about recycled
aggregate concrete (RAC). Six questions covering two aspects of RAC (shrinkage and shear
strength) are submitted under three prompting conditions of increasing specificity and repeated
three times each, giving 6 x 3 x 3 x 3 = 162 responses.

This repository contains the code that produced those responses: it builds the prompts, submits
them through the assistants' ordinary web interfaces, saves each fully rendered answer as HTML,
extracts the plain text and the metadata, and computes the run-to-run repeatability indicators.

## What is here — and what is not

Here: the prompts, the six questions, the three prompting conditions, and the collection and
analysis scripts.

Not here: the 162 collected answers, the expert scoring rubrics and the averaged expert scores.
Those are published as Supplementary Information with the article. No API keys, credentials or
browser profiles are stored in this repository, and none are needed to read it.

## Pipeline

```
questions/questions.json  +  config/conditions.yaml
            |
            |  scripts/build_prompts.py
            v
   outputs/study/prompts.json                       18 prompts (6 questions x C1/C2/C3)
            |
            |  scripts/collect_playwright.py        signed-in Chrome, driven over CDP
            v
   outputs/study/rac_html/<response_id>.html        the fully rendered answer, as displayed
            |
            |  scripts/ingest_html.py               plain text + metadata
            v
   outputs/study/captured.jsonl
            |
            |  scripts/analyse_repeatability.py     word counts + Voyage embeddings
            v
   outputs/study/eval/run_similarity.csv
```

Each response is identified as `<question_id>_<condition>_<assistant>_r<repetition>`, for
example `T1-02_C1_gemini_r3`.

## Prompting conditions

| Condition | Prompt composition | Intended effect |
|---|---|---|
| C1 – zero-shot | Question only; no role, context or instructions | Baseline; mirrors typical non-expert use |
| C2 – structured-expert | Expert persona plus prescribed answer headings | Elicit more domain-appropriate, structured answers |
| C3 – rule-grounded | C2 framing plus explicit, topic-specific technical rules | Ground the answer in codified design knowledge |

The exact C2 template and the C3 rule sets are in `scripts/build_prompts.py` and
`config/conditions.yaml`.

## Requirements

* Python 3.10 or newer
* Google Chrome
* `pip install -r requirements.txt`, then `python -m playwright install chromium`
* A free Voyage AI API key for the repeatability analysis only

## Use

Build the prompt pack:

```bash
python scripts/build_prompts.py
```

Sign in to the assistants once. This opens Chrome with a dedicated persistent profile
(`outputs/study/.pw_profile`, git-ignored) and keeps it open while you log in:

```bash
python scripts/collect_playwright.py login
```

Each assistant must be put into its no-history mode: ChatGPT opens in Temporary Chat through
its URL, Claude gets a fresh chat per prompt, and Gemini requires "Apps Activity" to be switched
off once in the Google account.

Collect the responses. The run is resumable — response ids already present in `captured.jsonl`
are skipped:

```bash
python scripts/collect_playwright.py run
python scripts/collect_playwright.py run --rounds 1 --tools chatgpt --limit 3   # smoke test
```

To attach to a Chrome you started yourself with `--remote-debugging-port=9222` instead of
launching one, add `--cdp http://localhost:9222`.

Extract text and metadata, then compute the repeatability indicators:

```bash
python scripts/ingest_html.py outputs/study/rac_html
export VOYAGE_API_KEY="..."          # PowerShell: $env:VOYAGE_API_KEY = "..."
python scripts/analyse_repeatability.py
```

`run_similarity.csv` holds one row per question x condition x assistant group: the three pairwise
cosine similarities between the repetitions, their mean, the three word counts and the
within-group word-count range.

Set `RAC_RUN_DIR` to work in a different run folder under `outputs/` (default `study`).

## Notes and limitations

* The collector reads the assistants' live web interfaces through CSS selectors. Those interfaces
  change without notice, so the selectors in `TOOLS` at the top of `scripts/collect_playwright.py`
  must be re-checked before any new collection.
* Free web tiers are not deterministic and are updated continuously. Re-running the collection
  will not reproduce the answers analysed in the article; it reproduces the *procedure*. The
  answers as collected in June 2026 are provided as Supplementary Information with the article.
* Microsoft Copilot and xAI Grok were tried during piloting and excluded from the final study;
  their selectors are still present in the collector but are not part of the published results.
* CAPTCHAs are never solved automatically. The collector pauses so that a human can solve one.

## Citation

The article is under review. Citation details will be added here once it is published.

## License

MIT — see [LICENSE](LICENSE).
