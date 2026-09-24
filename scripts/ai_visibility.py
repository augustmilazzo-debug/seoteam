"""Check whether AI assistants cite akarimedspa.com for the questions in
config/ai-visibility-prompts.txt.

Each engine runs only if its API key is set:
    ANTHROPIC_API_KEY   Claude with web search
    OPENAI_API_KEY      ChatGPT (Responses API with web search)
    PERPLEXITY_API_KEY  Perplexity Sonar
    GEMINI_API_KEY      Gemini with Google Search grounding

Usage:
    python scripts/ai_visibility.py
    python scripts/ai_visibility.py --engines perplexity,gemini --limit 5

Writes data/snapshots/ai-visibility-YYYY-MM-DD.json with, per prompt and engine:
cited (our domain in the sources), mentioned (brand named in the answer),
every cited domain (to spot competitors), and the answer text.
"""

import argparse
import datetime as dt
import json
import os
import pathlib
import re
import sys
from urllib.parse import urlparse

import requests

ROOT = pathlib.Path(__file__).resolve().parent.parent
DOMAIN = "akarimedspa.com"
BRAND_RE = re.compile(r"akari", re.I)
USER_LOCATION = "Newport Beach, California, US"
TIMEOUT = 120


def domain_of(url):
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def ask_claude(prompt):
    import anthropic

    client = anthropic.Anthropic()
    messages = [{"role": "user", "content": prompt}]
    tools = [{
        "type": "web_search_20260209",
        "name": "web_search",
        "max_uses": 5,
        "user_location": {"type": "approximate", "city": "Newport Beach", "region": "California", "country": "US"},
    }]
    # Server-side web search can pause long turns; resume a few times.
    for _ in range(4):
        resp = client.messages.create(model="claude-opus-5", max_tokens=16000, tools=tools, messages=messages)
        if resp.stop_reason != "pause_turn":
            break
        messages.append({"role": "assistant", "content": resp.content})
    if resp.stop_reason == "refusal":
        raise RuntimeError("model declined the request")
    text, urls = [], []
    for block in resp.content:
        if block.type == "text":
            text.append(block.text)
            urls += [c.url for c in (block.citations or []) if getattr(c, "url", None)]
        elif block.type == "web_search_tool_result" and isinstance(block.content, list):
            urls += [r.url for r in block.content]
    return "".join(text), urls


def ask_openai(prompt):
    resp = requests.post(
        "https://api.openai.com/v1/responses",
        headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"},
        json={
            "model": os.environ.get("OPENAI_MODEL", "gpt-5"),
            "tools": [{"type": "web_search", "user_location": {"type": "approximate", "city": "Newport Beach", "region": "California", "country": "US"}}],
            "input": prompt,
        },
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    text, urls = [], []
    for item in resp.json().get("output", []):
        for part in item.get("content", []) or []:
            if part.get("type") == "output_text":
                text.append(part.get("text", ""))
                urls += [a["url"] for a in part.get("annotations", []) if a.get("type") == "url_citation"]
    return "".join(text), urls


def ask_perplexity(prompt):
    resp = requests.post(
        "https://api.perplexity.ai/chat/completions",
        headers={"Authorization": f"Bearer {os.environ['PERPLEXITY_API_KEY']}"},
        json={"model": os.environ.get("PERPLEXITY_MODEL", "sonar"), "messages": [{"role": "user", "content": f"{prompt} (I live in {USER_LOCATION})"}]},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    urls = list(data.get("citations", [])) + [r["url"] for r in data.get("search_results", []) if r.get("url")]
    return data["choices"][0]["message"]["content"], urls


def ask_gemini(prompt):
    model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
    resp = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        headers={"x-goog-api-key": os.environ["GEMINI_API_KEY"]},
        json={"contents": [{"parts": [{"text": f"{prompt} (I live in {USER_LOCATION})"}]}], "tools": [{"google_search": {}}]},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    cand = resp.json()["candidates"][0]
    text = "".join(p.get("text", "") for p in cand.get("content", {}).get("parts", []))
    chunks = cand.get("groundingMetadata", {}).get("groundingChunks", [])
    # Gemini returns redirect URLs; the title carries the source domain.
    urls = [f"https://{c['web'].get('title', '')}" if "vertexaisearch" in c["web"].get("uri", "") else c["web"]["uri"] for c in chunks if "web" in c]
    return text, urls


ENGINES = {
    "claude": ("ANTHROPIC_API_KEY", ask_claude),
    "chatgpt": ("OPENAI_API_KEY", ask_openai),
    "perplexity": ("PERPLEXITY_API_KEY", ask_perplexity),
    "gemini": ("GEMINI_API_KEY", ask_gemini),
}


def load_prompts():
    lines = (ROOT / "config" / "ai-visibility-prompts.txt").read_text().splitlines()
    return [l.strip() for l in lines if l.strip() and not l.startswith("#")]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--engines", default=",".join(ENGINES))
    parser.add_argument("--limit", type=int, default=0, help="only run the first N prompts")
    args = parser.parse_args()

    engines = {n: fn for n, (key, fn) in ENGINES.items() if n in args.engines.split(",") and os.environ.get(key)}
    if not engines:
        sys.exit("No AI engine API keys are set; skipping AI visibility check.")
    prompts = load_prompts()
    if args.limit:
        prompts = prompts[: args.limit]

    results = []
    for prompt in prompts:
        for name, fn in engines.items():
            row = {"prompt": prompt, "engine": name}
            try:
                text, urls = fn(prompt)
                domains = sorted({domain_of(u) for u in urls if u})
                row.update(
                    cited=any(d == DOMAIN or d.endswith("." + DOMAIN) for d in domains),
                    mentioned=bool(BRAND_RE.search(text)),
                    cited_domains=domains,
                    answer=text[:4000],
                )
            except Exception as e:  # one failing engine must not stop the run
                row["error"] = f"{type(e).__name__}: {e}"[:500]
            results.append(row)
            status = row.get("error") or ("CITED" if row["cited"] else "mentioned" if row["mentioned"] else "absent")
            print(f"[{name:10}] {status:10} {prompt}")

    ok = [r for r in results if "error" not in r]
    summary = {
        "engines": list(engines),
        "checks": len(ok),
        "cited": sum(r["cited"] for r in ok),
        "mentioned": sum(r["mentioned"] for r in ok),
        "errors": len(results) - len(ok),
    }
    path = ROOT / "data" / "snapshots" / f"ai-visibility-{dt.date.today().isoformat()}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"generated": dt.date.today().isoformat(), "summary": summary, "results": results}, indent=1))
    print(f"Wrote {path.relative_to(ROOT)}: {summary}")


if __name__ == "__main__":
    main()
