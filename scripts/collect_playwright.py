"""Web-UI response collector for the RAC LLM benchmark (Playwright + system Chrome).

Drives a real, signed-in Chrome browser so that every prompt is submitted through the
assistant's ordinary free web interface, exactly as a non-expert user would. Chrome is
either launched with a dedicated persistent profile (outputs/<run>/.pw_profile, so the
sign-ins survive between runs) or attached to an already-running Chrome over the Chrome
DevTools Protocol with --cdp.

Two phases:
  login : opens the browser with one tab per assistant so you can sign in once. The script
          reports when each tab looks ready, then exits with the session saved to the profile.
  run   : drives the collection. Order is round-major across assistants and condition-major
          within a round. Resumable: response_ids already present in captured.jsonl are
          skipped. Writes captured.jsonl and rac_html/<response_id>.html per response.

Usage (RAC_RUN_DIR selects the run folder under outputs/, default "study"):
  python scripts/collect_playwright.py login
  python scripts/collect_playwright.py run
  python scripts/collect_playwright.py run --rounds 1 --tools chatgpt --limit 3   # smoke test

No-history mode is used throughout: ChatGPT Temporary Chat (via the URL), a fresh chat per
prompt for Claude, and Gemini with "Apps Activity" switched off in the Google account
(a one-time setting made in the browser).

CAPTCHAs are never solved automatically: the script pauses for up to ~3 minutes so you can
solve one by hand, then continues.
"""

from __future__ import annotations

import argparse
import html as htmllib
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
RUN = os.environ.get("RAC_RUN_DIR", "study")
BASE = ROOT / "outputs" / RUN
PROFILE = BASE / ".pw_profile"
HTML_DIR = BASE / "rac_html"
CAPTURED = BASE / "captured.jsonl"
PROMPTS = BASE / "prompts.json"
QUESTION_ORDER = ["T1-01", "T1-02", "T1-03", "T2-01", "T2-02", "T2-03"]
TOPIC = {"T1": "shrinkage", "T2": "shear"}

# Launch options that avoid the automation banner; some sign-in pages refuse a browser
# started with --enable-automation.
LAUNCH = dict(channel="chrome", headless=False, no_viewport=True,
              args=["--start-maximized", "--disable-blink-features=AutomationControlled"],
              ignore_default_args=["--enable-automation"])

TOOLS = {
    "chatgpt": {
        "url": "https://chatgpt.com/?temporary-chat=true",
        "composer": "#prompt-textarea",
        "send": 'button[data-testid="send-button"]',
        "stop": 'button[data-testid="stop-button"], button[aria-label="Stop streaming"]',
        "answer": '[data-message-author-role="assistant"]',
        "note": "ChatGPT free, Temporary Chat",
        "skip_link": r"chatgpt\.com|openai\.com",
        "clean": "",
    },
    "gemini": {
        "url": "https://gemini.google.com/app",
        "composer": ".ql-editor",
        "send": 'button[aria-label*="Send" i]',
        "stop": 'button[aria-label*="Stop" i]',
        "answer": ".markdown-main-panel",
        "note": "Gemini Flash, fresh chat, Apps Activity off",
        "skip_link": r"google\.com/?(search)?$|gemini\.google",
        "clean": r"\n*Was this visual helpful\?[\s\S]*$",
    },
    "copilot": {
        "url": "https://copilot.microsoft.com/",
        "composer": "textarea",
        "send": 'button[aria-label*="Submit" i], button[aria-label*="Send" i]',
        "stop": 'button[aria-label*="Stop" i], button[title*="Stop" i]',
        "answer": '[data-content="ai-message"]',
        "note": "Copilot Smart (free), Temporary",
        "skip_link": r"copilot\.microsoft|bing\.com/?$",
        "clean": r"^Copilot said\s*|\n*Edit in a page\s*$",
    },
    "grok": {
        "url": "https://grok.com/",
        "composer": ".tiptap.ProseMirror",
        "send": 'button[aria-label="Submit"]',
        "stop": 'button[aria-label*="Stop" i]',
        "answer": ".response-content-markdown",
        "note": "Grok Fast (free), Private chat",
        "skip_link": r"grok\.com|x\.com/?$|twitter\.com",
        "clean": "",
        "inject": "paste",   # TipTap/ProseMirror: keyboard typing doesn't register the submit
        "pick": "last",       # user + assistant share .response-content-markdown; assistant is last
    },
    "claude": {
        "url": "https://claude.ai/new",
        "composer": ".tiptap.ProseMirror",
        "send": 'button[aria-label="Send message"]',
        "stop": '[data-is-streaming="true"]',
        "answer": "[data-is-streaming]",
        "note": "Claude free (claude.ai), fresh chat",
        "skip_link": r"claude\.ai|anthropic\.com",
        "clean": "",
        "inject": "paste",
    },
}


def load_prompts() -> dict:
    data = json.loads(PROMPTS.read_text(encoding="utf-8"))
    return {(p["question_id"], p["condition"]): p["prompt"] for p in data}


def done_ids() -> set:
    ids = set()
    if CAPTURED.exists():
        for line in CAPTURED.read_text(encoding="utf-8").splitlines():
            if line.strip():
                ids.add(json.loads(line)["response_id"])
    return ids


def dismiss_modals(page):
    try:
        page.evaluate(
            "[...document.querySelectorAll('button')].forEach(b=>{if((b.textContent||'').trim()==='Dismiss')b.click();})")
    except Exception:
        pass


def has_captcha(page) -> bool:
    try:
        return page.evaluate("/verify (you are )?human|are you human|captcha/i.test(document.body.innerText||'')")
    except Exception:
        return False


def composer_ready(page, sel, timeout=8000) -> bool:
    try:
        page.wait_for_selector(sel, timeout=timeout, state="visible")
        return True
    except Exception:
        return False


def insert_prompt(page, T, prompt):
    page.locator(T["composer"]).first.click()
    if T.get("inject") == "paste":
        page.evaluate(
            """([s,t])=>{const e=document.querySelector(s);e.focus();
               const dt=new DataTransfer();dt.setData('text/plain',t);
               e.dispatchEvent(new ClipboardEvent('paste',{clipboardData:dt,bubbles:true,cancelable:true}));}""",
            [T["composer"], prompt])
        page.wait_for_timeout(500)
        return
    page.keyboard.insert_text(prompt)
    page.wait_for_timeout(400)
    # fallback for contenteditables if insert_text didn't register
    val = page.evaluate("(s)=>{const e=document.querySelector(s);return (e.value!==undefined?e.value:e.innerText)||''}", T["composer"])
    if len(val.strip()) < min(10, len(prompt)):
        page.evaluate(
            """([s,t])=>{const e=document.querySelector(s);e.focus();
               const dt=new DataTransfer();dt.setData('text/plain',t);
               e.dispatchEvent(new ClipboardEvent('paste',{clipboardData:dt,bubbles:true,cancelable:true}));}""",
            [T["composer"], prompt])
        page.wait_for_timeout(400)


def click_send(page, T):
    try:
        btn = page.locator(T["send"]).first
        btn.wait_for(state="visible", timeout=4000)
        if btn.is_enabled():
            btn.click()
            return
    except Exception:
        pass
    page.keyboard.press("Enter")


def _pick_js():
    # JS snippet: choose answer element, skipping any that are just the echoed prompt
    return """const els=[...document.querySelectorAll(asel)];
        const isPrompt=(e)=>{const t=(e.innerText||'').trim().toLowerCase(); return pnorm && t && (t===pnorm || (t.length<=pnorm.length+6 && pnorm.indexOf(t)>=0));};
        let el=null;
        if(pick==='last'){ for(let i=els.length-1;i>=0;i--){ if(isPrompt(els[i])) continue; el=els[i]; break; } }
        else { el=els.filter(e=>!isPrompt(e)).sort((a,b)=>(a.innerText||'').length-(b.innerText||'').length).pop(); }"""


def answer_state(page, T, prompt=""):
    return page.evaluate(
        "([asel,ssel,pick,pnorm])=>{" + _pick_js() +
        "return {len:el?(el.innerText||'').trim().length:0, streaming:!!document.querySelector(ssel)};}",
        [T["answer"], T["stop"], T.get("pick", "largest"), (prompt or "").strip().lower()])


def wait_stable(page, T, prompt="", max_s=130):
    last, stable, t0 = -1, 0, time.time()
    while time.time() - t0 < max_s:
        page.wait_for_timeout(2000)
        dismiss_modals(page)
        if has_captcha(page):
            print("    CAPTCHA detected; pausing up to 180s for a manual solve...", flush=True)
            ct = time.time()
            while has_captcha(page) and time.time() - ct < 180:
                page.wait_for_timeout(3000)
        try:
            st = answer_state(page, T, prompt)
        except Exception:
            continue
        if not st["streaming"] and st["len"] > 40 and st["len"] == last:
            stable += 1
            if stable >= 3:
                return True
        else:
            stable = 0
        last = st["len"]
    return last > 40  # accept whatever we have if it stopped growing


def extract(page, T, prompt=""):
    return page.evaluate(
        """([asel,skip,clean,pick,pnorm])=>{
            const els=[...document.querySelectorAll(asel)];
            const isPrompt=(e)=>{const t=(e.innerText||'').trim().toLowerCase(); return pnorm && t && (t===pnorm || (t.length<=pnorm.length+6 && pnorm.indexOf(t)>=0));};
            let el=null;
            if(pick==='last'){ for(let i=els.length-1;i>=0;i--){ if(isPrompt(els[i])) continue; el=els[i]; break; } }
            else { el=els.filter(e=>!isPrompt(e)).sort((a,b)=>(a.innerText||'').length-(b.innerText||'').length).pop(); }
            if(!el) return {text:'', html:'', refs:[]};
            let text=(el.innerText||'').trim();
            if(clean){ try{ text=text.replace(new RegExp(clean,'ig'),'').trim(); }catch(e){} }
            const seen=new Set(), refs=[];
            document.querySelectorAll(asel+' a[href], [class*=\"source\" i] a[href], [class*=\"citation\" i] a[href]').forEach(a=>{
              const u=(a.href||'').trim();
              if(/^https?:\\/\\//i.test(u) && !(new RegExp(skip,'i')).test(u) && !seen.has(u)){
                seen.add(u); refs.push({title:(a.textContent||a.getAttribute('aria-label')||'').trim().slice(0,200), url:u});}});
            return {text, html:el.innerHTML, refs};
        }""", [T["answer"], T["skip_link"], T["clean"], T.get("pick", "largest"), (prompt or "").strip().lower()])


def save(rid, qid, cond, tool, rnd, prompt, ext):
    HTML_DIR.mkdir(parents=True, exist_ok=True)
    text = ext["text"]
    wc = len(text.split())
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    rec = {"response_id": rid, "tool": tool, "model_note": TOOLS[tool]["note"],
           "collected_date": now, "mistake": False, "text": text,
           "references": ext["refs"], "word_count": wc}
    with CAPTURED.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    data = {"response_id": rid, "question_id": qid, "topic": TOPIC[qid.split("-")[0]],
            "condition": cond, "tool": tool, "round": rnd, "model_note": TOOLS[tool]["note"],
            "prompt": prompt, "answer_text": text, "answer_html": ext["html"],
            "references": ext["refs"], "word_count": wc, "collected_at": now}
    refs_li = "".join(f'<li><a href="{htmllib.escape(r["url"],True)}">{htmllib.escape(r.get("title") or r["url"])}</a></li>'
                      for r in ext["refs"])
    page_html = (
        '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><title>' + rid + '</title>'
        '<script type="application/json" id="rac-data">' + json.dumps(data, ensure_ascii=False) + '</script>'
        '<style>body{font:14px/1.55 system-ui,Segoe UI,Arial,sans-serif;margin:24px;color:#111}'
        'h1{font-size:18px;margin:0 0 4px}.kv{color:#444;font-size:13px;margin-bottom:14px}.kv b{color:#111}'
        '.sec{font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:#666;margin:16px 0 4px}'
        '.prompt{background:#f5f5f5;border:1px solid #ddd;border-radius:6px;padding:10px;white-space:pre-wrap}'
        '.answer{border:1px solid #e3e3e3;border-radius:6px;padding:12px}.answer,.prompt{overflow-wrap:break-word;word-break:break-word}'
        'ol{padding-left:20px}</style></head><body>'
        f'<h1>{rid}</h1><div class="kv">Tool: <b>{tool}</b> | Question: <b>{qid}</b> ({TOPIC[qid.split("-")[0]]}) | '
        f'Condition: <b>{cond}</b> | Round: <b>{rnd}</b> | Words: <b>{wc}</b><br>Model/setup: {TOOLS[tool]["note"]} '
        f'(fresh no-memory chat) {now}</div>'
        f'<div class="sec">Prompt sent</div><div class="prompt">{htmllib.escape(prompt)}</div>'
        f'<div class="sec">Model answer (verbatim)</div><div class="answer">{ext["html"]}</div>'
        f'<div class="sec">Sources / references</div><ol>{refs_li}</ol></body></html>')
    (HTML_DIR / f"{rid}.html").write_text(page_html, encoding="utf-8")
    return wc


def enable_copilot_temporary(page):
    try:
        page.evaluate("""()=>{
          const t=[...document.querySelectorAll('button,[role=switch],label')].find(e=>/temporary/i.test(e.getAttribute('aria-label')||e.textContent||''));
          if(t && t.getAttribute('aria-checked')!=='true') t.click();
        }""")
    except Exception:
        pass


def collect_one(page, tool, qid, cond, rnd, prompt):
    T = TOOLS[tool]
    page.goto(T["url"], wait_until="domcontentloaded")
    page.wait_for_timeout(1500)
    dismiss_modals(page)
    if tool == "copilot":
        enable_copilot_temporary(page)
    if tool == "grok":
        try:
            page.evaluate("""()=>{const e=document.querySelector('.ot-pc-refuse-all-handler')||document.querySelector('#onetrust-reject-all-handler');if(e)e.click();}""")
        except Exception:
            pass
    if not composer_ready(page, T["composer"], timeout=15000):
        return None, "no composer (login? page error?)"
    insert_prompt(page, T, prompt)
    click_send(page, T)
    page.wait_for_timeout(2000)
    wait_stable(page, T, prompt)
    ext = extract(page, T, prompt)
    if not ext["text"] or len(ext["text"]) < 30:
        return None, f"empty/short answer (len={len(ext['text'])})"
    # reject when the answer element was actually the echoed prompt (no real answer rendered)
    at = ext["text"].strip().lower().rstrip("?.")
    pt = prompt.strip().lower().rstrip("?.")
    if at == pt or (len(at) < 220 and at in pt):
        return None, "captured the prompt, not the answer"
    rid = f"{qid}_{cond}_{tool}_r{rnd}"
    wc = save(rid, qid, cond, tool, rnd, prompt, ext)
    return wc, None


def login_state(pg, tool) -> str:
    """Best-effort sign-in detection (informational; the user confirms completion)."""
    try:
        return pg.evaluate("""()=>{
          const txt=(document.body.innerText||'');
          const hasComposer=!!document.querySelector('#prompt-textarea, .ql-editor, textarea');
          const signinBtn=[...document.querySelectorAll('a,button')].some(e=>/^\\s*(log in|sign in|log in)\\s*$/i.test((e.textContent||'').trim()));
          const avatar=!!document.querySelector('img[alt*="Account" i], [aria-label*="Account" i], [data-testid*="profile" i], img[alt*="avatar" i]');
          if(avatar && hasComposer) return 'logged-in';
          if(signinBtn) return 'NOT-signed-in';
          return hasComposer ? 'maybe' : 'loading';
        }""")
    except Exception:
        return "?"


def cmd_login(tools):
    sentinel = BASE / ".login_done"
    if sentinel.exists():
        sentinel.unlink()
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(str(PROFILE), **LAUNCH)
        ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
        pages = []
        for t in tools:
            pg = ctx.new_page()
            pg.goto(TOOLS[t]["url"], wait_until="domcontentloaded")
            pages.append((t, pg))
        print("Browser open. Sign in to each tool (and turn OFF Gemini 'Apps Activity').", flush=True)
        print(f"This window stays open until you signal done (create file: {sentinel}) or 30 min.", flush=True)
        t0, last_print = time.time(), 0.0
        while time.time() - t0 < 1800:
            if sentinel.exists():
                break
            if time.time() - last_print > 20:
                states = {t: login_state(pg, t) for t, pg in pages}
                print("  state: " + ", ".join(f"{t}={s}" for t, s in states.items()), flush=True)
                last_print = time.time()
            time.sleep(3)
        ctx.close()  # graceful close flushes cookies to the profile
        print(f"login window closed; profile saved at {PROFILE}", flush=True)


def cmd_run(rounds, tools, conditions, limit, cdp=None):
    prompts = load_prompts()
    done = done_ids()
    plan = []
    for rnd in rounds:
        for tool in tools:
            for cond in conditions:
                for qid in QUESTION_ORDER:
                    rid = f"{qid}_{cond}_{tool}_r{rnd}"
                    if rid not in done:
                        plan.append((tool, qid, cond, rnd, prompts[(qid, cond)]))
    if limit:
        plan = plan[:limit]
    print(f"plan: {len(plan)} responses to collect (run={RUN})", flush=True)
    if not plan:
        return
    with sync_playwright() as pw:
        if cdp:
            browser = pw.chromium.connect_over_cdp(cdp)
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
        else:
            ctx = pw.chromium.launch_persistent_context(str(PROFILE), **LAUNCH)
            ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
            page = ctx.new_page()
        # hard caps so no single Playwright call can hang the whole run
        page.set_default_timeout(30000)
        page.set_default_navigation_timeout(45000)
        ok = fail = 0
        for i, (tool, qid, cond, rnd, prompt) in enumerate(plan, 1):
            rid = f"{qid}_{cond}_{tool}_r{rnd}"
            try:
                wc, err = collect_one(page, tool, qid, cond, rnd, prompt)
                if err:
                    fail += 1
                    print(f"  [{i}/{len(plan)}] FAIL {rid}: {err}", flush=True)
                else:
                    ok += 1
                    print(f"  [{i}/{len(plan)}] OK   {rid}  ({wc} words)", flush=True)
            except Exception as e:
                fail += 1
                print(f"  [{i}/{len(plan)}] ERROR {rid}: {e}", flush=True)
            page.wait_for_timeout(1500)
        print(f"done. ok={ok} fail={fail} total_saved={len(done_ids())}", flush=True)
        if not cdp:
            ctx.close()


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    lp = sub.add_parser("login")
    lp.add_argument("--tools", default="chatgpt,gemini,claude")
    rp = sub.add_parser("run")
    rp.add_argument("--rounds", default="1,2,3")
    rp.add_argument("--tools", default="chatgpt,gemini,claude")
    rp.add_argument("--conditions", default="C1,C2,C3")
    rp.add_argument("--limit", type=int, default=0)
    rp.add_argument("--cdp", default=None, help="attach to a running Chrome via CDP, e.g. http://localhost:9222")
    a = ap.parse_args()
    if a.cmd == "login":
        cmd_login(a.tools.split(","))
    else:
        cmd_run([int(x) for x in a.rounds.split(",")], a.tools.split(","),
                a.conditions.split(","), a.limit, a.cdp)


if __name__ == "__main__":
    main()
