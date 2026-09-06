# llm-rac-benchmark

Collection toolchain for the study *"We taught LLMs, but can they teach us? Insights from
recycled aggregate concrete and beyond"*.

The study evaluates what the freely available web tiers of three conversational assistants,
ChatGPT (OpenAI), Gemini (Google) and Claude (Anthropic), answer when asked about recycled
aggregate concrete (RAC). Six questions covering two aspects of RAC (shrinkage and shear
strength) are submitted under three prompting conditions of increasing specificity and repeated
three times each, giving 6 x 3 x 3 x 3 = 162 responses.

This repository contains the code that produced those responses. It builds the prompts, submits
them through the assistants' ordinary web interfaces in a no-history session, saves each fully
rendered answer as HTML, extracts the plain text and the metadata, and computes the run-to-run
repeatability indicators.

## What is here, and what is not

Here: the prompts, the six questions, the three prompting conditions, and the collection and
analysis scripts.

Not here: the 162 collected answers, the expert scoring rubrics and the averaged expert scores.
Those are published as Supplementary Information with the article. No API keys, credentials or
browser profiles are stored in this repository. The one external service used, the embedding
API, is configured with your own key at run time (see below).

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
            |  scripts/analyse_repeatability.py     word counts + embedding similarity
            v
   outputs/study/eval/run_similarity.csv
```

Each response is identified as `<question_id>_<condition>_<assistant>_r<repetition>`, for
example `T1-02_C1_gemini_r3`.

## Prompting conditions

| Condition | Prompt composition | Intended effect |
|---|---|---|
| C1, zero-shot | The question on its own; no role, context or instructions | Baseline; mirrors typical non-expert use |
| C2, structured-expert | An expert persona, plus a request to state confidence, boundary conditions and open debates, and to close with a summary, the key assumptions and what remains uncertain | Elicit more domain-appropriate, structured answers |
| C3, rule-grounded | The question preceded by a short list of explicit, topic-specific rules the answer must observe. C3 does not carry the C2 persona | Force the answer to disaggregate the distinctions that matter, rather than averaging over them |

The exact wording of each condition is in `scripts/build_prompts.py`, and the C3 rule sets are in
`config/conditions.yaml`. The C3 rules are expert-set requirements to treat cases separately, for
example recycled concrete aggregate against mixed recycled aggregate, fine against coarse
replacement, and basic against drying shrinkage. They are not quotations from a design code.

The generated prompts are in `outputs/study/prompts.json` and are byte-identical to those
submitted in the published collection; `scripts/build_prompts.py` reproduces that file exactly.

---

## How it works

### Why a real browser instead of an API

The object of study is what the *free web tier* tells an ordinary user, not what a model can do
in principle. The public APIs serve different model versions, different system prompts and
different defaults from the web interface, so an API run would not answer the question. The
collector therefore drives a real Chrome that is signed in exactly as a person would be, either
by launching one with a dedicated persistent profile or by attaching over the Chrome DevTools
Protocol to a Chrome you started yourself with `--remote-debugging-port=9222`.

Everything each assistant needs is described declaratively in the `TOOLS` table at the top of
`scripts/collect_playwright.py`: entry URL, the composer element, the send and stop buttons, the
element holding the answer, and a regular expression for interface text to strip from the answer.
Adding a fourth assistant means adding one entry to that table.

### No-history collection, and why it matters

Every response has to be an independent sample. If an assistant remembers the earlier
repetitions, the second and third answers are conditioned on the first, the measured
repeatability is inflated, and the experiment quietly stops measuring what it claims to measure.
Each assistant is therefore run with its own history mechanism switched off:

| Assistant | Mode | How it is set |
|---|---|---|
| ChatGPT | Temporary Chat | entered through `chatgpt.com/?temporary-chat=true`; the conversation is not written to history and is not used for personalisation |
| Gemini | Apps Activity off | a one-time setting in the Google account; conversations are not retained and not fed into later answers |
| Claude | Incognito chat | `claude.ai/new` is opened for every prompt and the incognito control is switched on, so the conversation is not saved and there is no cross-chat memory |

On top of that, the collector navigates to the entry URL before *every single prompt*, so no two
prompts ever share a conversation thread, and each captured answer is checked against the prompt
text so that an echoed prompt is never mistaken for an answer.

The incognito control is located by its accessible label rather than by a fixed CSS selector,
because these interfaces rebuild their markup frequently. If no such control is found the
collector says so and continues, since a fresh chat per prompt already prevents cross-chat
memory. In the published June 2026 collection Claude was run with a fresh chat per prompt; the
incognito step was added afterwards and additionally prevents the conversation from being stored
on the provider's side.

This is stricter than a private or incognito browser window. An incognito window only discards
state on your own machine; the settings above stop the provider from retaining the conversation
on its side, which is what actually determines whether the next answer is influenced.

It also has a cost worth stating plainly: with history off, nothing is saved, so a conversation
cannot be reopened afterwards to recover something that was not captured at the time. Whatever
you need must be extracted while the answer is still on screen.

### What is captured

For each prompt the collector waits until the answer has finished streaming, defined as the
rendered text length staying unchanged across three consecutive checks with no stop button
present, which prevents half-streamed answers from being saved. It then writes one
self-contained HTML file per response containing:

* a machine-readable metadata block, `<script type="application/json" id="rac-data">`, holding
  the assistant, question id, topic, condition, repetition, model note, timestamp and word count;
* the prompt exactly as sent;
* the answer's rendered DOM, verbatim;
* any source or citation links found in or beside the answer.

That metadata block is what `scripts/ingest_html.py` reads, so each HTML file is at the same time
a human-readable record and the machine input to the analysis. Runs are resumable: response ids
already present in `captured.jsonl` are skipped, and re-ingesting is idempotent.

### Measuring repeatability with embeddings

Two answers to the same question are almost never worded the same way, so surface measures such
as string overlap or edit distance report large differences even when the content is identical.
An embedding model maps each answer to a vector whose direction encodes meaning, so the cosine of
the angle between two answer vectors measures whether they *say* the same thing, independently of
phrasing.

`scripts/analyse_repeatability.py` embeds each of the three repetitions of a
question-condition-assistant group once, then reports the three pairwise cosine similarities and
their mean as percentages, together with the three word counts and their range. Both are needed:
in this study the answers turned out to be highly repeatable in meaning, around 97%, while their
length varied by 101 to 227 words on average depending on the assistant. Stable meaning does not
imply stable form.

The default embedding model is Voyage AI `voyage-3.5`, chosen because it is a current
general-purpose text embedding model with a free tier that requires no payment method.

### Bring your own key, or your own model

No key is bundled. Provide one in the environment:

```bash
export VOYAGE_API_KEY="..."           # PowerShell: $env:VOYAGE_API_KEY = "..."
```

Any Voyage embedding model can be selected with `--model`, or by setting `VOYAGE_MODEL`.

To use a different provider entirely, replace one function, `voyage_embed()` in
`scripts/analyse_repeatability.py`. It takes a list of strings and returns a list of vectors in
the same order, and nothing downstream depends on the provider. The similarity computation
itself, `cosine()`, and the grouping logic are plain Python with no third-party dependencies, so
a locally hosted model works just as well as a hosted API.

The free Voyage tier is rate-limited to roughly 3 requests and 10,000 tokens per minute. The
script batches inputs within a token budget and, on the first rate-limit response, drops to about
one request per minute so that a run completes instead of failing. `--max-tokens`, `--max-items`
and `--min-interval` control this.

---

## Requirements

* Python 3.10 or newer
* An installed Google Chrome. The collector drives Chrome itself (`channel="chrome"`), not
  Playwright's bundled Chromium, because the assistants' sign-in flows expect a real Chrome.
* `pip install -r requirements.txt`. Playwright is the only third-party dependency; everything
  else is in the standard library. No browser download step is needed.
* An embedding API key, for the repeatability analysis only

## Use

Build the prompt pack:

```bash
python scripts/build_prompts.py
```

Sign in to the assistants once. This opens Chrome with a dedicated persistent profile
(`outputs/study/.pw_profile`, git-ignored) and keeps it open while you log in and set the
no-history modes described above:

```bash
python scripts/collect_playwright.py login
```

Collect the responses. The run is resumable, so it can be interrupted and restarted:

```bash
python scripts/collect_playwright.py run
python scripts/collect_playwright.py run --rounds 1 --tools chatgpt --limit 3
python scripts/collect_playwright.py run --cdp http://localhost:9222
```

Extract text and metadata, then compute the repeatability indicators:

```bash
python scripts/ingest_html.py outputs/study/rac_html
python scripts/analyse_repeatability.py
```

`run_similarity.csv` holds one row per question, condition and assistant group: the three
pairwise cosine similarities between the repetitions, their mean, the three word counts and the
within-group word-count range. A companion `run_similarity_by_id.json` maps each response id to
its group mean, for joining back onto individual responses.

Set `RAC_RUN_DIR` to work in a different run folder under `outputs/` (default `study`).

## Notes and limitations

* The collector reads the assistants' live web interfaces through CSS selectors. Those interfaces
  change without notice, so the selectors in `TOOLS` must be re-checked before any new collection.
* Free web tiers are not deterministic and are updated continuously. Re-running the collection
  will not reproduce the answers analysed in the article; it reproduces the *procedure*. The
  answers as collected in June 2026 are provided as Supplementary Information with the article.
* Microsoft Copilot and xAI Grok were tried during piloting and excluded from the final study.
  Their selectors are still present in the collector but are not part of the published results.
* CAPTCHAs are never solved automatically. The collector pauses so that a person can solve one.

## Citation

The article is under review. Citation details will be added here once it is published.

## License

MIT, see [LICENSE](LICENSE).
