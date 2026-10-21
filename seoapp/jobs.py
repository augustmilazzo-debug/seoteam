"""Background work: the audit pipeline, rank checks, content generation, and
the scheduler that keeps every client re-audited automatically."""

import concurrent.futures as cf
import datetime as dt
import re
import threading
import time
import traceback

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import select

from . import ai, audit, config, crawler, locations, overlap, scoring, sources
from .db import Alert, BlogPost, Client, GbpPost, Job, Keyword, Page, Recommendation, SessionLocal, now

REVIEW_CYCLE_DAYS = 28  # every ranking page gets a full AI review at least this often
MIN_REVIEWS, MAX_REVIEWS = 6, 25  # AI page reviews per audit, to spread cost evenly
LOCAL_HINT = re.compile(r"\bnear me\b|\bnearby\b", re.I)
_wake = threading.Event()


# -- queue --------------------------------------------------------------------
def enqueue(client_id: int, kind: str, **params) -> int:
    with SessionLocal() as db:
        active = db.scalars(select(Job).where(Job.client_id == client_id, Job.kind == kind,
                                              Job.status.in_(["queued", "running"]))).all()
        same = next((j for j in active if (j.params or {}) == params), None)
        if same:  # already queued with the same inputs (e.g. a double click)
            return same.id
        job = Job(client_id=client_id, kind=kind, params=params)
        db.add(job)
        db.commit()
        _wake.set()
        return job.id


class JobLog:
    def __init__(self, db, job: Job):
        self.db, self.job = db, job
        self.warnings = 0

    def __call__(self, msg: str):
        line = f"[{now():%H:%M:%S}] {msg}"
        print(f"job {self.job.id}: {line}", flush=True)
        self.job.log = (self.job.log + line + "\n")[-20000:]
        self.db.commit()


def worker_loop():
    with SessionLocal() as db:  # jobs interrupted by a restart
        for j in db.scalars(select(Job).where(Job.status == "running")):
            j.status, j.log = "queued", j.log + "(restarted)\n"
        db.commit()
    while True:
        with SessionLocal() as db:
            job = db.scalar(select(Job).where(Job.status == "queued").order_by(Job.id))
            if not job:
                db.close()
                _wake.wait(30)
                _wake.clear()
                continue
            job.status, job.started_at = "running", now()
            db.commit()
            log = JobLog(db, job)
            try:
                HANDLERS[job.kind](db, db.get(Client, job.client_id), log, **(job.params or {}))
                job.status = "warning" if log.warnings else "done"
            except Exception as e:  # keep the worker alive; show the error in the UI
                db.rollback()
                job = db.get(Job, job.id)
                log.job = job
                log(f"FAILED: {e}")
                log("Technical details:\n" + traceback.format_exc()[-3000:])
                job.status = "failed"
            job.finished_at = now()
            db.commit()


# -- helpers ------------------------------------------------------------------
def page_dict(p: Page) -> dict:
    d = {c: getattr(p, c) for c in (
        "url", "status_code", "redirect_to", "title", "meta_description", "h1", "h2", "word_count",
        "canonical", "robots_meta", "indexable", "schema_types", "links_out", "inlinks", "images_total",
        "images_missing_alt", "has_viewport", "lang", "og_title", "response_ms", "depth", "text",
        "pagespeed", "extra")}
    d["non_html"] = (p.extra or {}).get("non_html", False)
    return d


def html_pages(c: Client) -> list[Page]:
    return [p for p in c.pages if p.status_code == 200 and p.indexable and p.title is not None
            and not (p.extra or {}).get("non_html")]


def ranked_pages(c: Client) -> list[Page]:
    """Most important pages first: home, then by inbound internal links."""
    home = crawler.normalize(c.url)
    return sorted(html_pages(c), key=lambda p: (p.url.rstrip("/") != home.rstrip("/"), p.depth, -p.inlinks))


def upsert_rec(db, c: Client, data: dict, kw_priority: int = 0) -> Recommendation:
    score, level = scoring.compute(data["impact"], data["effort"], data["confidence"], kw_priority, data["category"])
    r = db.scalar(select(Recommendation).where(Recommendation.client_id == c.id,
                                               Recommendation.fingerprint == data["fingerprint"]))
    if r is None:
        r = Recommendation(client_id=c.id, fingerprint=data["fingerprint"])
        db.add(r)
    elif r.status == "done":
        if r.category in ("technical", "on_page", "local") and r.fingerprint.startswith("tech:"):
            r.status = "open"  # marked done, but the re-audit still finds the problem
            data = {**data, "details": "**Re-audit still detects this issue after it was marked done.**\n\n" + data["details"]}
        else:
            return r
    elif r.status in ("dismissed",):
        return r
    elif r.status == "resolved":
        r.status = "open"
    for k in ("category", "title", "details", "page_url", "keyword", "impact", "effort", "confidence", "location"):
        if k in data:
            setattr(r, k, data[k])
    r.needs_approval = data.get("needs_approval", r.needs_approval or False)
    r.score, r.level = score, level
    r.updated_at = r.last_seen_at = now()
    return r


def safe(step, db, c, log, *args, **kwargs):
    """Run one audit step; on error, log it and let the rest of the audit continue."""
    try:
        return step(db, c, log, *args, **kwargs)
    except Exception as e:
        db.rollback()
        log(f"WARNING: step '{step.__name__.removeprefix('step_')}' failed: {type(e).__name__}: {e}")
        log(traceback.format_exc()[-1500:])
        if hasattr(log, "warnings"):
            log.warnings += 1
        return None


def google_page(pos) -> int | None:
    return None if pos is None else int((pos - 1) // 10) + 1


def is_local(k: Keyword) -> bool:
    return bool(k.location) or bool(LOCAL_HINT.search(k.keyword)) or k.intent in ("transactional", "local")


def page_keywords(c: Client) -> dict[str, list[Keyword]]:
    """Tracked keywords grouped by the page meant to rank for them, primary first."""
    out: dict[str, list[Keyword]] = {}
    for k in c.keywords:
        if k.tracked and k.target_url:
            out.setdefault(k.target_url, []).append(k)
    for kws in out.values():
        kws.sort(key=lambda k: (k.role != "primary", -k.priority, -(k.search_volume or 0)))
    return out


def site_page_list(c: Client) -> list[str]:
    """'url | title | targets: ...' for every page, so the AI knows which page owns which topic."""
    targets: dict[str, list[str]] = {}
    for k in c.keywords:
        if k.target_url:
            targets.setdefault(k.target_url, []).append(k.keyword)
    return [f"{x.url} | {x.title}" + (f" | targets: {', '.join(targets[x.url][:3])}" if x.url in targets else "")
            for x in ranked_pages(c)]


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:80]


def kw_view(k: Keyword) -> dict:
    return {"keyword": k.keyword, "priority": k.priority, "position": k.position, "maps_position": k.local_pack_position,
            "search_volume": k.search_volume, "difficulty": k.difficulty, "target_url": k.target_url}


# -- pipeline steps -----------------------------------------------------------
def step_crawl(db, c: Client, log):
    log(f"Crawling {c.url} (up to {config.MAX_PAGES} pages)")
    res = crawler.Crawler(c.url, log=log).crawl()
    if res.errors:
        for e in res.errors:
            log(e)
    seen = set()
    existing = {p.url: p for p in c.pages}
    for d in res.pages:
        seen.add(d["url"])
        p = existing.get(d["url"])
        if p is None:
            p = Page(client_id=c.id, url=d["url"])
            db.add(p)
            c.pages.append(p)
        if p.content_hash and d.get("content_hash") and p.content_hash != d["content_hash"]:
            p.changed_at = now()
        for k in ("status_code", "redirect_to", "title", "meta_description", "h1", "h2", "word_count",
                  "canonical", "robots_meta", "indexable", "schema_types", "links_out", "inlinks",
                  "images_total", "images_missing_alt", "has_viewport", "lang", "og_title", "response_ms",
                  "depth", "text", "content_hash"):
            if k in d:
                setattr(p, k, d[k])
        if d.get("status_code") != 200:
            p.title = p.title or ""
        p.extra = {**(p.extra or {}), "phones": d.get("phones", []), "has_address_text": d.get("has_address_text", False),
                   "non_html": d.get("non_html", False), "error": d.get("error", "")}
        p.last_crawled = now()
    for url, p in existing.items():
        if url not in seen:
            db.delete(p)
            c.pages.remove(p)
    db.commit()
    log(f"Crawl finished: {len(res.pages)} URLs, {len(html_pages(c))} indexable HTML pages")
    return res.site


def step_pagespeed(db, c: Client, log):
    targets = ranked_pages(c)[:3]
    tracked_urls = {k.target_url for k in c.keywords if k.tracked and k.target_url}
    targets += [p for p in html_pages(c) if p.url in tracked_urls and p not in targets][:2]
    for p in targets:
        ps = sources.pagespeed(p.url)
        if ps:
            p.pagespeed = ps
            log(f"PageSpeed {p.url}: {ps['performance']}/100")
        else:
            log(f"PageSpeed unavailable for {p.url}")
    db.commit()


def step_profile(db, c: Client, log, force=False):
    if c.profile and not force:
        return
    if not ai.enabled():
        log("Skipping business profile: add ANTHROPIC_API_KEY to enable AI features")
        return
    log("Reading the site to build the business profile (services, locations)")
    pages = [page_dict(p) for p in ranked_pages(c)]
    c.profile = ai.build_profile(pages)
    if c.profile.get("business_name") and (not c.name or c.name == sources.domain_of(c.url)):
        c.name = c.profile["business_name"]
    locs = c.profile.get("locations") or []
    if locs and not c.rank_location:
        c.rank_location = locs[0].get("dataforseo_location", "")
    db.commit()
    log(f"Profile: {len(c.profile.get('services', []))} services, {len(locs)} location(s)")


def step_gsc(db, c: Client, log):
    if not sources.gsc_ready(c):
        log("Search Console not connected for this client (client → Settings → Connect Google Search Console)")
        return []
    try:
        rows = sources.gsc_rows(c)
    except Exception as e:
        log(f"Search Console error: {e}")
        c.gsc_cache = {**(c.gsc_cache or {}), "fetched": dt.date.today().isoformat(), "error": str(e)}
        db.commit()
        return []
    rows.sort(key=lambda r: -r["impressions"])
    prev = {}
    try:
        for r in sources.gsc_rows(c, days=28, offset_days=28, dimensions=("page",)):
            prev[r["page"]] = {"clicks": r["clicks"], "impressions": r["impressions"]}
    except Exception as e:
        log(f"Search Console (previous period) unavailable: {e}")
    c.gsc_cache = {"fetched": dt.date.today().isoformat(), "rows": rows[:3000], "prev_pages": prev}
    by_query: dict[str, dict] = {}
    for r in rows:
        q = by_query.setdefault(r["query"].lower(), {"clicks": 0, "impressions": 0, "pos_w": 0.0, "page": r["page"], "best": r})
        q["clicks"] += r["clicks"]
        q["impressions"] += r["impressions"]
        q["pos_w"] += r["position"] * r["impressions"]
        if r["impressions"] > q["best"]["impressions"]:
            q["best"], q["page"] = r, r["page"]
    for k in c.keywords:
        q = by_query.get(k.keyword.lower())
        if q:
            k.gsc_clicks, k.gsc_impressions = q["clicks"], q["impressions"]
            if not sources.dfs_enabled():
                alert = _record_position(k, round(q["pos_w"] / max(1, q["impressions"]), 1), None, q["page"], "gsc")
                if alert:
                    db.add(alert)
    db.commit()
    log(f"Search Console: {len(rows)} query/page rows")
    return rows


EXPECTED_CTR = {1: 0.28, 2: 0.15, 3: 0.10, 4: 0.07, 5: 0.05, 6: 0.04, 7: 0.03, 8: 0.025, 9: 0.02, 10: 0.018}


def _url_key(u: str) -> str:
    u = crawler.normalize(u)
    return re.sub(r"^https?://(www\.)?", "", u).rstrip("/").lower()


def step_gsc_insights(db, c: Client, log):
    """Turn Search Console data into page metrics, alerts, recommendations and keyword ideas."""
    cache = c.gsc_cache or {}
    rows = cache.get("rows") or []
    if not rows:
        return
    pages = {_url_key(p.url): p for p in html_pages(c)}
    brand = overlap.brand_words(c)
    # 1. Per-page metrics (stored for the Pages view)
    agg: dict[str, dict] = {}
    for r in rows:
        a = agg.setdefault(_url_key(r["page"]), {"clicks": 0, "impressions": 0, "pos_w": 0.0, "queries": []})
        a["clicks"] += r["clicks"]
        a["impressions"] += r["impressions"]
        a["pos_w"] += r["position"] * r["impressions"]
        a["queries"].append(r)
    prev = {_url_key(u): v for u, v in (cache.get("prev_pages") or {}).items()}
    for key, p in pages.items():
        a, pv = agg.get(key), prev.get(key, {})
        if not a:
            p.extra = {**(p.extra or {}), "gsc": {"clicks": 0, "impressions": 0, "clicks_prev": pv.get("clicks", 0),
                                                  "impressions_prev": pv.get("impressions", 0), "queries": []}}
            continue
        top = sorted(a["queries"], key=lambda r: -r["impressions"])[:10]
        p.extra = {**(p.extra or {}), "gsc": {
            "clicks": round(a["clicks"]), "impressions": round(a["impressions"]),
            "ctr": round(a["clicks"] / a["impressions"], 4) if a["impressions"] else 0,
            "position": round(a["pos_w"] / a["impressions"], 1) if a["impressions"] else None,
            "clicks_prev": round(pv.get("clicks", 0)), "impressions_prev": round(pv.get("impressions", 0)),
            "queries": [{"q": r["query"], "clicks": round(r["clicks"]), "impressions": round(r["impressions"]),
                         "position": round(r["position"], 1)} for r in top]}}
    seen = set()
    # 2. Low click-through: shown often on page 1, rarely clicked -> title/description rewrite
    for key, p in pages.items():
        g = (p.extra or {}).get("gsc") or {}
        pos, imp = g.get("position"), g.get("impressions", 0)
        if not pos or pos > 10 or imp < 150:
            continue
        expected = EXPECTED_CTR.get(max(1, round(pos)), 0.018)
        if g["ctr"] < expected * 0.5:
            fp = f"rule:gsc_ctr:{p.url}"
            seen.add(fp)
            missed = round(imp * expected - g["clicks"])
            qs = "\n".join(f"- “{q['q']}”: {q['impressions']} impressions, {q['clicks']} clicks, position {q['position']}"
                           for q in g["queries"][:6])
            upsert_rec(db, c, {
                "fingerprint": fp, "category": "on_page", "page_url": p.url, "keyword": (g["queries"] or [{}])[0].get("q", ""),
                "title": f"Few clicks despite page-1 visibility: rewrite the title and description of {p.url.split('//', 1)[-1]}",
                "details": f"Google showed this page **{imp:,} times** in 28 days at an average position of **{pos}**, but only "
                           f"**{g['clicks']}** people clicked (**{g['ctr']:.1%}** vs about {expected:.0%} typical at that position). "
                           f"That's roughly **{missed} missed visits a month**.\n\nCurrent title: “{p.title}”\n\n"
                           f"Current description: “{p.meta_description or '(none)'}”\n\nSearches it shows for:\n{qs}\n\n"
                           "Rewrite both to match those searches: lead with the treatment and city, add a concrete benefit "
                           "or proof point (rating, provider credentials), and a reason to click now.",
                "impact": min(9, 5 + imp // 1000), "effort": 1, "confidence": 0.8}, 4)
    # 3. Two pages showing for the same search (cannibalization)
    by_q: dict[str, list] = {}
    for r in rows:
        by_q.setdefault(r["query"], []).append(r)
    competing = []
    for q, rs in by_q.items():
        total = sum(r["impressions"] for r in rs)
        if total < 50 or overlap.tokens(q) <= brand or not overlap.tokens(q, brand):
            continue
        big = [r for r in rs if r["impressions"] >= total * 0.2]
        if len(big) >= 2:
            competing.append((total, q, big))
    for total, q, big in sorted(competing, reverse=True)[:10]:
        fp = f"rule:gsc_cannibal:{slug(q)}"
        seen.add(fp)
        lines = "\n".join(f"- {r['page']}: {round(r['impressions'])} impressions, position {round(r['position'], 1)}"
                          for r in sorted(big, key=lambda r: -r["impressions"]))
        upsert_rec(db, c, {
            "fingerprint": fp, "category": "on_page", "keyword": q, "page_url": max(big, key=lambda r: r["impressions"])["page"],
            "title": f"{len(big)} pages compete for “{q}”",
            "details": f"Google splits “{q}” ({round(total)} impressions in 28 days) between these pages, which "
                       f"usually holds both back:\n\n{lines}\n\nPick one page to own this search. Make it clearly the best "
                       "match (title, H1, intro), have the other page link to it with this phrase as anchor text, and "
                       "trim that topic from the other page. If the pages are near-duplicates, merge them with a 301 redirect.",
            "impact": 6, "effort": 3, "confidence": 0.7}, 3)
    # 4. Traffic drops vs the previous 28 days
    recent = now() - dt.timedelta(days=7)
    for key, p in pages.items():
        g = (p.extra or {}).get("gsc") or {}
        before, after = g.get("clicks_prev", 0), g.get("clicks", 0)
        if before >= 20 and after <= before * 0.7:
            drop = round((1 - after / before) * 100)
            fp = f"rule:gsc_drop:{p.url}"
            seen.add(fp)
            upsert_rec(db, c, {
                "fingerprint": fp, "category": "on_page", "page_url": p.url,
                "title": f"Google clicks to {p.url.split('//', 1)[-1]} fell {drop}%",
                "details": f"Clicks from Google: **{before} → {after}** (last 28 days vs the 28 before). Check whether the page "
                           "changed recently, whether a competitor overtook it (Pages tab → Open → top-3 comparison), and the "
                           "searches it lost in Search Console. The weekly page review will prioritize this page.",
                "impact": 7, "effort": 3, "confidence": 0.7}, 4)
            msg = f"Google clicks down {drop}% ({before} → {after}) vs the previous 28 days"
            if not any(a.page_url == p.url and a.message == msg and a.created_at > recent for a in c.alerts):
                db.add(Alert(client_id=c.id, kind="down", keyword="(all searches)", page_url=p.url, message=msg))
    # 5. Pages Google doesn't show at all
    total_imp = sum(r["impressions"] for r in rows)
    invisible = [p for p in pages.values() if (p.extra or {}).get("rank_worthy", True)
                 and not ((p.extra or {}).get("gsc") or {}).get("impressions") and p.depth < 99]
    if total_imp >= 500 and invisible:
        fp = "rule:gsc_invisible"
        seen.add(fp)
        upsert_rec(db, c, {
            "fingerprint": fp, "category": "technical",
            "title": f"{len(invisible)} page(s) got zero Google impressions in 28 days",
            "details": "These pages never appeared in Google results. Check each in Search Console → URL Inspection: if "
                       "it's not indexed, request indexing and add internal links to it; if it is indexed, it's too thin "
                       "or too similar to another page to rank.\n\n" + "\n".join(f"- {p.url}" for p in invisible[:30]),
            "impact": 5, "effort": 3, "confidence": 0.7}, 3)
    for r in c.recommendations:
        if r.fingerprint.startswith("rule:gsc_") and r.fingerprint not in seen and r.status in ("open", "approved"):
            r.status, r.result, r.done_at = "resolved", "No longer detected in Search Console data.", now()
    # 6. Real searches worth targeting -> keyword suggestions
    have = {k.keyword.lower() for k in c.keywords}
    ideas = []
    for q, rs in by_q.items():
        imp = sum(r["impressions"] for r in rs)
        pos = sum(r["position"] * r["impressions"] for r in rs) / imp if imp else 99
        if imp >= 30 and pos <= 30 and q.lower() not in have and overlap.tokens(q, brand):
            ideas.append((imp, q, rs, pos))
    added = 0
    for imp, q, rs, pos in sorted(ideas, reverse=True)[:40]:
        best = max(rs, key=lambda r: r["impressions"])
        page = pages.get(_url_key(best["page"]))
        db.add(Keyword(client_id=c.id, keyword=q.lower(), source="gsc", tracked=False, priority=3,
                       target_url=page.url if page else "", gsc_impressions=round(imp),
                       gsc_clicks=round(sum(r["clicks"] for r in rs))))
        added += 1
    db.commit()
    log(f"Search Console insights: {len(seen)} issue(s) flagged, {added} new keyword idea(s) from real searches")


def _record_position(k: Keyword, pos, local, url, source):
    """Store a new position. Returns an Alert for notable moves (not on the first check)."""
    old, first_check = k.position, k.last_checked is None
    if k.position is not None:
        k.previous_position = k.position
    k.position = pos
    k.local_pack_position = local if local is not None else (k.local_pack_position if source == "gsc" else None)
    k.ranking_url = url or ""
    if pos is not None and (k.best_position is None or pos < k.best_position):
        k.best_position = pos
    k.history = (k.history or []) + [{"date": dt.date.today().isoformat(), "position": pos, "local": local, "source": source}]
    k.history = k.history[-120:]
    k.last_checked = now()
    if first_check:
        return None
    msg, kind = "", ""
    if old is None and pos is not None:
        kind, msg = "new", f"Now ranking #{_fmt(pos)} (page {google_page(pos)})"
    elif old is not None and pos is None:
        kind, msg = "lost", f"Dropped out of the top {sources.RANK_DEPTH} (was #{_fmt(old)})"
    elif old is not None and pos is not None:
        delta = old - pos
        page_change = google_page(old) != google_page(pos)
        if abs(delta) >= 3 or page_change:
            kind = "up" if delta > 0 else "down"
            msg = f"{'Up' if delta > 0 else 'Down'} {_fmt(abs(delta))} to #{_fmt(pos)}"
            if page_change:
                msg += f" (now on page {google_page(pos)})"
    if not kind:
        return None
    return Alert(client_id=k.client_id, kind=kind, keyword=k.keyword, page_url=k.target_url or k.ranking_url,
                 old_position=old, new_position=pos, message=msg)


def _fmt(x) -> str:
    return str(int(x)) if float(x).is_integer() else str(round(x, 1))


def step_keywords(db, c: Client, log, force=False):
    if not ai.enabled():
        log("Skipping keyword generation: add ANTHROPIC_API_KEY")
        return
    if any(k.source == "ai" for k in c.keywords) and not force:
        return
    log("Generating target keywords from services and locations")
    gsc_q = sorted(((r["query"], r["impressions"], round(r["position"], 1)) for r in (c.gsc_cache or {}).get("rows", [])),
                   key=lambda x: -x[1])[:80]
    ideas = ai.generate_keywords(c.profile or {}, [page_dict(p) for p in ranked_pages(c)],
                                 [k.keyword for k in c.keywords],
                                 [{"query": q, "impressions": i, "position": p} for q, i, p in gsc_q])
    have = {k.keyword.lower() for k in c.keywords}
    valid_urls = {p.url for p in c.pages}
    added = []
    for idea in ideas:
        kw = idea["keyword"].strip().lower()
        if not kw or kw in have:
            continue
        have.add(kw)
        k = Keyword(client_id=c.id, keyword=kw, service=idea["service"], location=idea["location"],
                    intent=idea["intent"], source="ai", priority=max(1, min(5, idea["priority"])),
                    target_url=idea["target_url"] if idea["target_url"] in valid_urls else "",
                    tracked=idea["priority"] >= 4)
        db.add(k)
        c.keywords.append(k)
        added.append(k)
    db.commit()
    log(f"Added {len(added)} keyword ideas ({sum(k.tracked for k in added)} auto-tracked at priority 4-5; adjust on the Keywords tab)")
    step_metrics(db, c, log, added)


def step_page_keywords(db, c: Client, log, force=False):
    """Give every rank-worthy page a primary keyword (+ up to 2 secondary) and track them."""
    if not ai.enabled():
        return
    pages = [p for p in ranked_pages(c) if force or "kw_assigned" not in (p.extra or {})]
    if not pages:
        return
    gsc_by_page: dict[str, list] = {}
    for r in (c.gsc_cache or {}).get("rows", []):
        gsc_by_page.setdefault(r["page"], []).append({"query": r["query"], "impressions": r["impressions"],
                                                      "position": round(r["position"], 1)})
    by_kw = {k.keyword.lower(): k for k in c.keywords}
    has_primary = {k.target_url for k in c.keywords if k.role == "primary"}
    log(f"Choosing target keywords for {len(pages)} page(s)")
    added = []
    for i in range(0, len(pages), 25):
        batch = pages[i:i + 25]
        taken = [k.keyword for k in c.keywords if k.target_url]
        targets = ai.assign_page_keywords(c.profile or {}, [page_dict(p) for p in batch], gsc_by_page, taken)
        by_url = {t["url"]: t for t in targets}
        for p in batch:
            t = by_url.get(p.url)
            p.extra = {**(p.extra or {}), "kw_assigned": dt.date.today().isoformat(),
                       "rank_worthy": bool(t and t["rank_worthy"] and t["primary_keyword"])}
            if not p.extra["rank_worthy"]:
                continue
            wanted = [(t["primary_keyword"], "primary")] + [(kw, "secondary") for kw in t["secondary_keywords"][:2]]
            for kw, role in wanted:
                kw = kw.strip().lower()
                if not kw:
                    continue
                if role == "primary" and p.url in has_primary:
                    role = "secondary"  # keep the primary the owner already chose
                k = by_kw.get(kw)
                if k is None:
                    k = Keyword(client_id=c.id, keyword=kw, source="page", location=t["location"] if role == "primary" else "",
                                priority=max(1, min(5, t["priority"])), tracked=True, target_url=p.url, role=role)
                    db.add(k)
                    c.keywords.append(k)
                    by_kw[kw] = k
                    added.append(k)
                elif not k.target_url or k.target_url == p.url:
                    k.target_url, k.tracked = p.url, True
                    k.role = k.role or role
                if role == "primary":
                    has_primary.add(p.url)
        db.commit()
    log(f"Page targets set: {len(added)} new keyword(s) tracked across {len(has_primary)} page(s)")
    step_metrics(db, c, log, added)


def step_metrics(db, c: Client, log, kws=None):
    kws = kws if kws is not None else [k for k in c.keywords if k.search_volume is None]
    if not kws or not sources.dfs_enabled():
        return
    try:
        m = sources.keyword_metrics([k.keyword for k in kws])
    except Exception as e:
        log(f"Keyword volume lookup failed: {e}")
        return
    for k in kws:
        d = m.get(k.keyword.lower(), {})
        k.search_volume = d.get("search_volume")
        k.cpc = d.get("cpc")
        k.difficulty = d.get("difficulty")
    db.commit()
    log(f"Search volume/difficulty updated for {len(kws)} keywords")


def step_map(db, c: Client, log):
    unmapped = [k for k in c.keywords if k.tracked and not k.target_url]
    if not unmapped or not ai.enabled():
        return
    mapping = ai.map_keywords([k.keyword for k in unmapped], [page_dict(p) for p in ranked_pages(c)])
    valid = {p.url for p in c.pages}
    for k in unmapped:
        u = mapping.get(k.keyword.lower(), "")
        if u in valid:
            k.target_url = u
    db.commit()


def step_technical(db, c: Client, site: dict, log):
    pages = [page_dict(p) for p in c.pages]
    findings = audit.run_checks(site, pages, c.profile)
    found = set()
    for f in findings:
        upsert_rec(db, c, f)
        found.add(f["fingerprint"])
    resolved = 0
    for r in c.recommendations:
        if r.fingerprint.startswith("tech:") and r.fingerprint not in found and r.status in ("open", "approved"):
            r.status, r.result = "resolved", f"Not detected in the {dt.date.today()} re-audit."
            r.done_at = now()
            resolved += 1
    db.commit()
    log(f"Technical audit: {len(findings)} issue groups found, {resolved} previously open issue(s) now fixed")


def _competitor_summaries(k: Keyword, own_domain: str, log) -> list[dict]:
    comps = [x for x in (k.competitors or []) if x.get("url") and own_domain not in (x.get("domain") or "")][:3]

    def fetch(comp):
        try:
            d = crawler.Crawler(comp["url"], max_pages=1, log=lambda *_: None).fetch(crawler.normalize(comp["url"]), 0)
        except Exception:
            return None
        if d.get("status_code") != 200:
            return {"rank": comp.get("rank"), "url": comp["url"], "domain": comp.get("domain"), "title": comp.get("title"),
                    "fetched": False}
        return {"rank": comp.get("rank"), "url": comp["url"], "domain": comp.get("domain"), "title": d.get("title"),
                "h1": d.get("h1"), "h2": (d.get("h2") or [])[:20], "word_count": d.get("word_count"),
                "schema": d.get("schema_types"), "faq": "FAQPage" in (d.get("schema_types") or []), "fetched": True}

    with cf.ThreadPoolExecutor(max_workers=3) as pool:
        return [x for x in pool.map(fetch, comps) if x]


def step_wrong_page(db, c: Client, log):
    """Flag keywords where Google ranks a different page than the one meant to rank."""
    norm = lambda u: (u or "").rstrip("/").lower()
    seen = set()
    for k in c.keywords:
        if not (k.tracked and k.target_url and k.ranking_url) or norm(k.ranking_url) == norm(k.target_url):
            continue
        fp = f"rule:wrong_page:{slug(k.keyword)}"
        seen.add(fp)
        upsert_rec(db, c, {
            "fingerprint": fp, "category": "on_page", "keyword": k.keyword, "page_url": k.target_url,
            "title": f"Google ranks the wrong page for “{k.keyword}”",
            "details": f"Google shows **{k.ranking_url}** (position {k.position}) for “{k.keyword}”, but the page meant "
                       f"to rank is **{k.target_url}**. Two pages competing splits their strength.\n\n"
                       "Fix: make the target page clearly the best match (keyword in title, H1 and opening paragraph), "
                       f"link from {k.ranking_url} to the target page with the anchor text “{k.keyword}”, and remove or "
                       "reduce that keyword's focus on the other page. If the other page is actually the better fit, "
                       "change the target page for this keyword on the Keywords tab instead.",
            "impact": 6, "effort": 2, "confidence": 0.7}, k.priority)
    for r in c.recommendations:
        if r.fingerprint.startswith("rule:wrong_page:") and r.fingerprint not in seen and r.status in ("open", "approved"):
            r.status, r.result, r.done_at = "resolved", "Google now ranks the intended page.", now()
    db.commit()


def _cooling_down(c: Client, url: str) -> bool:
    cutoff = now() - dt.timedelta(days=config.PAGE_COOLDOWN_DAYS)
    return any(r.page_url == url and r.status == "done" and r.done_at and r.done_at > cutoff
               and r.category in ("on_page", "content") for r in c.recommendations)


def _review_page(db, c: Client, p: Page, kws: list[Keyword], log):
    main = kws[0]
    competitors = (main.analysis or {}).get("competitors") or _competitor_summaries(main, sources.domain_of(c.url), log)
    gsc = [r for r in (c.gsc_cache or {}).get("rows", []) if r["page"] == p.url]
    site_pages = site_page_list(c)
    log(f"Reviewing {p.url} for {', '.join(k.keyword for k in kws)}")
    review = ai.optimize_page(c.profile or {}, page_dict(p), [kw_view(k) for k in kws], competitors, gsc, site_pages)
    for r in [r for r in c.recommendations if r.page_url == p.url and r.fingerprint.startswith("ai:page:") and r.status == "open"]:
        db.delete(r)  # replace stale, unapproved suggestions for this page
    prio = max(k.priority for k in kws)
    for r in review["recommendations"]:
        cat = r["category"] if r["category"] in ("on_page", "content", "local", "technical") else "on_page"
        upsert_rec(db, c, {**r, "category": cat, "page_url": p.url, "keyword": r.get("keyword") or main.keyword,
                           "fingerprint": f"ai:page:{p.url}:{slug(r['title'])}",
                           "needs_approval": cat == "content"}, prio)
    p.extra = {**(p.extra or {}), "optimized_hash": p.content_hash, "optimized_at": dt.date.today().isoformat(),
               "main_gap": review["main_gap"], "diagnosis": review["diagnosis"]}
    db.commit()


def step_optimize(db, c: Client, log):
    """Review pages on a rotation so every ranking page is reviewed at least every REVIEW_CYCLE_DAYS,
    most urgent first: recent drops, positions 4-20, never reviewed."""
    if not ai.enabled():
        log("Skipping AI page reviews: add ANTHROPIC_API_KEY")
        return
    by_page = page_keywords(c)
    pages = {p.url: p for p in c.pages}
    recent_drops = {a.page_url for a in c.alerts if a.kind in ("down", "lost") and now() - a.created_at < dt.timedelta(days=14)}
    budget = max(MIN_REVIEWS, min(MAX_REVIEWS, -(-len(by_page) * c.audit_interval_days // REVIEW_CYCLE_DAYS)))

    def urgency(url):
        kws = by_page[url]
        p = pages[url]
        last = (p.extra or {}).get("optimized_at")
        age = (dt.date.today() - dt.date.fromisoformat(last)).days if last else 999
        g = (p.extra or {}).get("gsc") or {}
        pos = min((k.position for k in kws if k.position), default=None) or g.get("position")
        striking = 2.0 if pos and 4 <= pos <= 20 else 1.3 if pos and pos > 20 else 1.2 if pos is None else 0.6
        demand = min(20, (g.get("impressions") or 0) / 200)  # pages Google already shows often are worth more
        return (url in recent_drops) * 100 + max(k.priority for k in kws) * striking * 4 + min(age, 60) + demand

    candidates = []
    for url in by_page:
        p = pages.get(url)
        if not p or p.status_code != 200:
            continue
        if _cooling_down(c, url):
            continue
        last = (p.extra or {}).get("optimized_at")
        fresh = last and (dt.date.today() - dt.date.fromisoformat(last)).days < REVIEW_CYCLE_DAYS
        if fresh and (p.extra or {}).get("optimized_hash") == p.content_hash and url not in recent_drops:
            continue  # reviewed recently, unchanged, not slipping
        candidates.append(url)
    done = 0
    for url in sorted(candidates, key=urgency, reverse=True)[:budget]:
        try:
            _review_page(db, c, pages[url], by_page[url], log)
            done += 1
        except Exception as e:
            db.rollback()
            log(f"WARNING: review of {url} failed: {e}")
            if hasattr(log, "warnings"):
                log.warnings += 1
    log(f"Page reviews: {done} of {len(by_page)} ranking pages reviewed this run "
        f"({len(candidates) - done} more due; every page is reviewed at least every {REVIEW_CYCLE_DAYS} days)")


def step_new_pages(db, c: Client, log):
    if not ai.enabled():
        return
    covered = {r.keyword.lower() for r in c.recommendations if r.category == "new_page" and r.status != "dismissed"}
    gaps = [k for k in c.keywords if k.tracked and not k.target_url and k.keyword.lower() not in covered]
    if not gaps:
        return
    pages_only = [it for it in overlap.inventory(c) if it["kind"] == "page"]
    real_gaps = []
    for k in sorted(gaps, key=lambda k: -k.priority):
        hit = overlap.matches(c, k.keyword, items=pages_only, threshold=overlap.DUPLICATE)
        if hit:  # an existing page already covers this search: strengthen it, don't add a competitor
            k.target_url = hit[0]["url"]
            log(f"'{k.keyword}' is already covered by {hit[0]['url']}; assigned it there instead of a new page")
        else:
            real_gaps.append(k)
    db.commit()
    gaps = real_gaps[:8]
    if not gaps:
        return
    log(f"Planning new pages for {len(gaps)} keyword(s) with no page")
    recs = ai.new_page_briefs(c.profile or {}, [kw_view(k) for k in gaps], site_page_list(c), overlap.prompt_inventory(c))
    prio = {k.keyword.lower(): k.priority for k in gaps}
    items = overlap.inventory(c)
    for r in recs:
        kw = (r.get("keyword") or "").lower()
        cat = "content" if r["category"] == "content" else "new_page"
        if cat == "new_page":
            dup = overlap.matches(c, kw or r["title"], r["title"], items=items, threshold=overlap.DUPLICATE)
            if dup:
                log(f"Skipped new page '{r['title']}': overlaps {dup[0]['label']} ({dup[0]['url']})")
                continue
        upsert_rec(db, c, {**r, "category": cat, "fingerprint": f"ai:new:{slug(kw or r['title'])}",
                           "needs_approval": True}, prio.get(kw, 3))
        items.append({"kind": "planned page", "label": r["title"], "url": "", "texts": [r["title"]],
                      "keywords": [kw] if kw else []})
    db.commit()


QUESTION = re.compile(r"^(how|what|why|when|is|are|can|does|do|which|who|should|best|cost|price)\b|\bvs\b|\bcost\b", re.I)


def step_blog(db, c: Client, log, force=False):
    """Keep the blog idea queue stocked from the target keywords."""
    if not ai.enabled():
        return
    # One-time move of older blog recommendations into the blog engine
    for r in c.recommendations:
        if r.category == "blog" and r.status == "open":
            db.add(BlogPost(client_id=c.id, title=r.title, target_keyword=r.keyword, angle=r.details,
                            priority=3, source="ai"))
            r.status, r.result = "dismissed", "Moved to the Blog tab."
    db.commit()
    waiting = [b for b in c.blog_posts if b.status == "idea"]
    if len(waiting) >= 4 and not force:
        return
    by_page = page_keywords(c)
    kws = [{**kw_view(k), "page": url} for url, ks in by_page.items() for k in ks]
    kws.sort(key=lambda k: (-k["priority"], -(k["search_volume"] or 0)))
    questions = sorted({r["query"] for r in (c.gsc_cache or {}).get("rows", []) if QUESTION.search(r["query"])})
    log("Planning blog posts from the target keywords")
    ideas = ai.blog_plan(c.profile or {}, kws, overlap.prompt_inventory(c), questions)
    valid = {p.url for p in c.pages}
    items = overlap.inventory(c)
    domain = sources.domain_of(c.url)
    added, skipped = [], []
    for i in ideas:
        kw = i["target_keyword"].strip().lower()
        dup = overlap.matches(c, kw, i["title"], items=items, threshold=overlap.DUPLICATE)
        if dup:
            skipped.append(f"'{i['title']}' (overlaps {dup[0]['kind']}: {dup[0]['label']})")
            continue
        if sources.dfs_enabled():
            try:  # does Google already rank one of our pages for this search?
                org = sources.organic_rank(kw, domain, c.rank_location, c.rank_device or "mobile")
            except sources.DataForSEOAccountError:
                raise
            except Exception:
                org = {}
            pos = org.get("position")
            ranking = next((it for it in items if it["url"].rstrip("/") == (org.get("url") or "").rstrip("/")), None)
            on_topic = ranking and overlap.matches(c, kw, i["title"], items=[ranking], threshold=overlap.SIMILAR)
            if pos and (pos <= 10 or (pos <= 20 and on_topic)):
                skipped.append(f"'{i['title']}' (the site already ranks #{org['position']} with {org['url']})")
                upsert_rec(db, c, {
                    "fingerprint": f"ai:strengthen:{slug(kw)}", "category": "content", "keyword": kw,
                    "page_url": org["url"], "title": f"Strengthen {org['url'].split('//', 1)[-1]} for “{kw}”",
                    "details": f"Instead of a new blog post, improve the page that already ranks **#{org['position']}** for "
                               f"“{kw}” so the site doesn't split its strength across two pages.\n\n"
                               f"Suggested angle from the blog planner: {i['angle']}\n\nOutline to fold in:\n\n{i['outline']}",
                    "impact": 6, "effort": 4, "confidence": 0.7, "needs_approval": True}, i["priority"])
                continue
        items.append({"kind": "blog idea", "label": i["title"], "url": "", "texts": [i["title"]], "keywords": [kw]})
        loc_key = next((l["key"] for l in locations.all_for(c) if i.get("location") and
                        i["location"].lower() in (l.get("city") or l["label"]).lower()), "")
        b = BlogPost(client_id=c.id, source="ai", title=i["title"], target_keyword=kw, location=loc_key,
                     secondary_keywords=i["secondary_keywords"], search_intent=i["search_intent"], angle=i["angle"],
                     outline=i["outline"], supports_url=i["supports_url"] if i["supports_url"] in valid else "",
                     priority=max(1, min(5, i["priority"])))
        db.add(b)
        added.append(b)
    db.commit()
    if added and sources.dfs_enabled():
        try:
            m = sources.keyword_metrics([b.target_keyword for b in added])
            for b in added:
                b.search_volume = m.get(b.target_keyword, {}).get("search_volume")
            db.commit()
        except Exception as e:
            log(f"Blog keyword volumes unavailable: {e}")
    log(f"Added {len(added)} blog idea(s)")
    if skipped:
        log(f"Skipped {len(skipped)} idea(s) that would compete with existing content: " + "; ".join(skipped))


def job_blog_write(db, c: Client, log, post_id: int, rev: str = ""):
    """rev is the rewrite request (so a new request isn't merged with a queued one)."""
    if not ai.enabled():
        raise RuntimeError("Add ANTHROPIC_API_KEY to write posts")
    b = db.get(BlogPost, post_id)
    b.status, b.updated_at = "writing", now()
    db.commit()
    competitors = []
    if sources.dfs_enabled() and b.target_keyword:
        try:
            place = locations.get(c, b.location)["rank_location"] if b.location else c.rank_location
            org = sources.organic_rank(b.target_keyword, sources.domain_of(c.url), place, c.rank_device or "mobile")
            tmp = Keyword(keyword=b.target_keyword, competitors=org["top"])
            competitors = _competitor_summaries(tmp, sources.domain_of(c.url), log)
            log(f"Studied {len(competitors)} page(s) ranking for '{b.target_keyword}'")
        except Exception as e:
            log(f"Could not study competing pages: {e}")
    post = {"title": b.title, "target_keyword": b.target_keyword, "secondary_keywords": b.secondary_keywords,
            "search_intent": b.search_intent, "angle_or_owner_notes": b.angle, "outline": b.outline,
            "supporting_service_page": b.supports_url, "revision_feedback": b.feedback,
            "location_focus": ({k: v for k, v in locations.get(c, b.location).items() if k in ("label", "address", "service_areas")}
                               if b.location else "all locations"),
            "previous_draft": b.body[:6000] if b.feedback and b.body else ""}
    related = overlap.matches(c, b.target_keyword or b.title, b.title, items=overlap.inventory(c, exclude_post_id=b.id))
    for r in related:
        if r["score"] >= overlap.DUPLICATE:
            log(f"Warning: this topic overlaps {r['kind']} '{r['label']}' ({r['url']}). Writing it to be clearly different.")
    log(f"Writing '{b.title}' (usually 2-4 minutes)")
    try:
        art = ai.write_blog(c.profile or {}, post, site_page_list(c), competitors,
                            [{"title": r["label"], "url": r["url"], "targets": r["keywords"]} for r in related[:8]])
    except Exception:
        b.status = "review" if b.body else "idea"
        db.commit()
        raise
    b.title, b.meta_title, b.meta_description = art["title"], art["meta_title"], art["meta_description"]
    b.slug, b.excerpt, b.answer_summary = slug(art["slug"] or art["title"]), art["excerpt"], art["answer_summary"]
    b.key_takeaways, b.body, b.faqs = art["key_takeaways"], art["body_markdown"], art["faqs"]
    b.visuals, b.references = art["visuals"], art["references"]
    b.category = art.get("category") or b.category
    b.status, b.feedback, b.updated_at = "review", "", now()
    db.commit()
    log("Draft ready for your review on the Blog tab")


def _location_keywords(c: Client, loc: dict) -> list[dict]:
    out = []
    for k in c.keywords:
        if k.tracked and any(l["key"] == loc["key"] for l in locations.for_keyword(c, k)):
            v = kw_view(k)
            if loc["key"] in (k.loc_ranks or {}):
                v["position"] = k.loc_ranks[loc["key"]]["position"]
                v["maps_position"] = k.loc_ranks[loc["key"]]["maps"]
            out.append(v)
    return sorted(out, key=lambda v: -v["priority"])


def step_gbp_audit(db, c: Client, log, force=False, location: str = ""):
    if not ai.enabled():
        return
    for loc in locations.all_for(c):
        if location and loc["key"] != location:
            continue
        g = locations.gbp_for(c, loc["key"])
        if not any(v for k, v in g.items() if not k.startswith("_")):
            continue
        last = g.get("_audited")
        if last and not force and (dt.date.today() - dt.date.fromisoformat(last)).days < 28:
            continue
        log(f"Auditing the Google Business Profile for {loc['label']}")
        recs = ai.gbp_audit(c.profile or {}, {"location": {k: loc.get(k) for k in ("label", "address", "phone", "service_areas")},
                                              **{k: v for k, v in g.items() if not k.startswith("_")}},
                            _location_keywords(c, loc), g.get("_maps_competitors", []))
        for r in recs:
            upsert_rec(db, c, {**r, "category": "gbp", "fingerprint": f"ai:gbp:{loc['key']}:{slug(r['title'])}",
                               "location": loc["key"]}, 4)
        locations.set_gbp(c, loc["key"], {**g, "_audited": dt.date.today().isoformat()})
        db.commit()


def step_measure(db, c: Client, log):
    """Record ranking change for work marked done 14+ days ago."""
    kws = {k.keyword.lower(): k for k in c.keywords}
    n = 0
    for r in c.recommendations:
        if r.status != "done" or r.result or not r.done_at or (now() - r.done_at).days < 14:
            continue
        k = kws.get((r.keyword or "").lower())
        before = (r.baseline or {}).get("position")
        if k and k.position is not None:
            if before:
                delta = before - k.position
                r.result = f"Position {before} -> {k.position} ({'+' if delta >= 0 else ''}{round(delta, 1)}) for '{k.keyword}'"
            else:
                r.result = f"Now ranking #{k.position} for '{k.keyword}'"
            n += 1
    db.commit()
    if n:
        log(f"Measured impact for {n} completed recommendation(s)")


def _check(c, k, place, domain, name, local):
    org = sources.organic_rank(k.keyword, domain, place, c.rank_device or "mobile", name)
    maps = sources.maps_rank(k.keyword, domain, name, place) if local else None
    return org, maps


def step_ranks(db, c: Client, log, page_url: str = "", keyword_id: int | None = None):
    tracked = [k for k in c.keywords if (k.tracked or k.id == keyword_id)
               and (not page_url or k.target_url == page_url) and (keyword_id is None or k.id == keyword_id)]
    if not tracked:
        log("No tracked keywords for this page yet" if page_url else "No tracked keywords yet")
        return
    if not sources.dfs_enabled():
        log("DataForSEO not connected: positions come from Search Console only")
        step_gsc(db, c, log)
        c.last_rank_at = now()
        db.commit()
        return
    domain = sources.domain_of(c.url)
    name = (c.profile or {}).get("business_name") or c.name
    locs = locations.all_for(c)
    maps_seen: dict[str, dict[str, int]] = {l["key"]: {} for l in locs}
    log(f"Checking {len(tracked)} keyword(s) on Google ({c.rank_device or 'mobile'}, top {sources.RANK_DEPTH}) "
        "and Maps (local keywords)" + (f" across {len(locs)} locations" if len(locs) > 1 else ""))
    alerts = failures = checked = 0
    bad_locations: set[str] = set()
    for k in tracked:
        place, check_locs = locations.rank_location_for(c, k)
        local = is_local(k) or bool(locations.city_in_keyword(c, k))
        per_loc, primary, snapshots = {}, None, {}
        try:
            if len(check_locs) == 1 or not local:
                org, maps = _check(c, k, place, domain, name, local)
                primary = (org, maps)
                snapshots[check_locs[0]["key"] if local else "main"] = (org, maps)
                for l in (check_locs[:1] if local else []):
                    per_loc[l["key"]] = {"position": org["position"], "maps": (maps or {}).get("position"),
                                         "pack": org["local_pack_position"]}
            else:  # generic local keyword ("botox near me"): search from each location
                for l in check_locs:
                    org, maps = _check(c, k, l["rank_location"], domain, name, True)
                    per_loc[l["key"]] = {"position": org["position"], "maps": (maps or {}).get("position"),
                                         "pack": org["local_pack_position"]}
                    snapshots[l["key"]] = (org, maps)
                    primary = primary or (org, maps)
        except sources.DataForSEOAccountError:
            raise  # login/billing problem: stop and show the reason instead of failing every keyword
        except sources.LocationError as e:
            bad_locations.add(str(e))
            log(f"Not checked '{k.keyword}': {e}")
            continue
        except Exception as e:
            log(f"Rank check failed for '{k.keyword}': {e}")
            failures += 1
            if failures >= 5 and checked == 0:
                raise RuntimeError(f"The first {failures} rank checks all failed. Last error: {e}")
            continue
        checked += 1
        org, maps = primary
        alert = _record_position(k, org["position"], (maps or {}).get("position"), org["url"], "dataforseo")
        if alert:
            db.add(alert)
            alerts += 1
        k.loc_ranks = per_loc
        k.competitors = [t for t in org["top"] if not t["ours"]][:5]
        k.serp = {key: {"location": o["location"], "device": o["device"], "checked": o["checked"], "position": o["position"],
                        "url": o["url"], "top": o["top"], "local_pack": o["local_pack"],
                        "local_pack_position": o["local_pack_position"], "check_url": o["check_url"],
                        "maps": m} for key, (o, m) in snapshots.items()}
        for key, (o, _) in snapshots.items():
            for x in o["local_pack"]:
                if not x["ours"] and key in maps_seen:
                    maps_seen[key][x["title"]] = maps_seen[key].get(x["title"], 0) + 1
        if k.role == "primary":
            k.analysis = {"checked": dt.date.today().isoformat(),
                          "competitors": _competitor_summaries(k, domain, log)}
        def fmt(v):
            s_ = f"Google {v['position'] or '>' + str(sources.RANK_DEPTH)}"
            if local:
                s_ += f", Map pack {v['pack'] or '-'}, Maps {v['maps'] or '>20'}"
            return s_
        where = "; ".join(f"{locations.get(c, key)['label']}: {fmt(v)}" for key, v in per_loc.items())
        log(f"'{k.keyword}' from {org['location']}: " + (where or f"Google {org['position'] or '>' + str(sources.RANK_DEPTH)}"))
        db.commit()
        time.sleep(0.2)
    for l in locs:
        seen = maps_seen[l["key"]]
        if seen:
            g = locations.gbp_for(c, l["key"])
            locations.set_gbp(c, l["key"], {**g, "_maps_competitors": sorted(seen, key=lambda t: -seen[t])[:8]})
    if alerts:
        log(f"{alerts} notable ranking change(s) recorded")
    if bad_locations:
        msg = " ".join(sorted(bad_locations))
        if hasattr(log, "warnings"):
            log.warnings += 1
        log(f"WARNING: some keywords were not checked because of their location. {msg}")
    if not page_url and keyword_id is None:
        c.last_rank_at = now()
    db.commit()


# -- job handlers -------------------------------------------------------------
def job_audit(db, c: Client, log, full=True):
    site = step_crawl(db, c, log)
    if site.get("platform") and (c.cms or "unknown") in ("unknown", "", "Other"):
        c.cms = site["platform"]
        db.commit()
        log(f"Website platform detected: {c.cms}")
    if not site.get("home_status"):
        step_technical(db, c, site, log)
        c.last_audit_at = now()
        db.commit()
        return
    safe(step_profile, db, c, log)
    safe(step_gsc, db, c, log)
    safe(step_gsc_insights, db, c, log)
    safe(step_keywords, db, c, log)
    safe(step_page_keywords, db, c, log)
    safe(step_metrics, db, c, log)
    safe(step_map, db, c, log)
    safe(step_pagespeed, db, c, log)
    safe(step_technical, db, c, site, log)
    if any(k.tracked for k in c.keywords) and sources.dfs_enabled() and ranks_due(c, slack_days=1):
        safe(step_ranks, db, c, log)
    safe(step_wrong_page, db, c, log)
    safe(step_optimize, db, c, log)
    safe(step_new_pages, db, c, log)
    safe(step_blog, db, c, log)
    safe(step_gbp_audit, db, c, log)
    safe(step_measure, db, c, log)
    c.last_audit_at = now()
    db.commit()
    warn = getattr(log, "warnings", 0)
    log("Audit complete" + (f" with {warn} warning(s): the steps above marked WARNING were skipped" if warn else ""))


def ranks_due(c: Client, slack_days: int = 0) -> bool:
    return not c.last_rank_at or now() - c.last_rank_at > dt.timedelta(days=c.rank_interval_days - slack_days)


def job_ranks(db, c, log, page_url: str = "", keyword_id: int | None = None):
    step_ranks(db, c, log, page_url=page_url, keyword_id=keyword_id)
    safe(step_wrong_page, db, c, log)
    safe(step_measure, db, c, log)


def job_review_page(db, c, log, page_id: int):
    if not ai.enabled():
        raise RuntimeError("Add ANTHROPIC_API_KEY to review pages")
    p = db.get(Page, page_id)
    kws = page_keywords(c).get(p.url)
    if not kws:
        raise RuntimeError("This page has no tracked keywords yet. Add one on the page screen first.")
    _review_page(db, c, p, kws, log)
    log("Review complete")


def job_gsc_sync(db, c, log):
    rows = step_gsc(db, c, log)
    if rows:
        step_gsc_insights(db, c, log)


def job_page_keywords(db, c, log):
    step_page_keywords(db, c, log, force=True)


def job_keywords(db, c, log):
    step_keywords(db, c, log, force=True)


def job_profile(db, c, log):
    step_profile(db, c, log, force=True)


def job_blog(db, c, log):
    step_blog(db, c, log, force=True)


def job_gbp_audit(db, c, log, location: str = ""):
    step_gbp_audit(db, c, log, force=True, location=location)


def job_gbp_posts(db, c, log, n=8, location: str = ""):
    if not ai.enabled():
        raise RuntimeError("Add ANTHROPIC_API_KEY to generate posts")
    loc = locations.get(c, location) if location else locations.all_for(c)[0]
    recent = [p.title for p in sorted(c.posts, key=lambda p: p.created_at, reverse=True)
              if (p.location or locations.all_for(c)[0]["key"]) == loc["key"]]
    month = dt.date.today().strftime("for %B")
    loc_pages = [p.url for p in c.pages if loc["key"] in p.url.lower()]
    posts = ai.gbp_posts(c.profile or {}, _location_keywords(c, loc), recent, n, month,
                         location={k: loc.get(k) for k in ("label", "address", "service_areas")} | {"pages": loc_pages[:10]}
                         if len(locations.all_for(c)) > 1 else None)
    for p in posts:
        db.add(GbpPost(client_id=c.id, post_type=p["post_type"], title=p["title"], body=p["body"],
                       cta=p["cta"], link=p["link"], keyword=p["keyword"], image_idea=p["image_idea"],
                       location=loc["key"],
                       suggested_date=dt.date.today() + dt.timedelta(days=max(0, p["days_from_now"]))))
    db.commit()
    log(f"Drafted {len(posts)} posts for {loc['label']} for approval")


def job_draft(db, c, log, rec_id: int, feedback: str = ""):
    if not ai.enabled():
        raise RuntimeError("Add ANTHROPIC_API_KEY to write drafts")
    r = db.get(Recommendation, rec_id)
    page = next((p for p in c.pages if p.url == r.page_url), None)
    log(f"Writing draft for: {r.title}")
    r.draft = ai.write_draft(c.profile or {}, {"title": r.title, "category": r.category, "keyword": r.keyword,
                                               "details": r.details}, page_dict(page) if page else None, feedback)
    r.draft_status = "draft"
    db.commit()
    log("Draft ready for review")


HANDLERS = {
    "audit": job_audit, "ranks": job_ranks, "keywords": job_keywords, "profile": job_profile,
    "blog": job_blog, "gbp_audit": job_gbp_audit, "gbp_posts": job_gbp_posts, "draft": job_draft,
    "review_page": job_review_page, "page_keywords": job_page_keywords, "blog_write": job_blog_write,
    "gsc_sync": job_gsc_sync,
}
JOB_LABELS = {
    "audit": "Full site audit", "ranks": "Rank check", "keywords": "Keyword ideas", "profile": "Business profile",
    "blog": "Blog ideas", "gbp_audit": "GBP audit", "gbp_posts": "GBP posts", "draft": "Content draft",
    "review_page": "Page review", "page_keywords": "Page keyword targets", "blog_write": "Blog post writing",
    "gsc_sync": "Search Console sync",
}


# -- scheduler ----------------------------------------------------------------
def tick():
    with SessionLocal() as db:
        for c in db.scalars(select(Client).where(Client.paused.is_(False))):
            if not c.last_audit_at or now() - c.last_audit_at > dt.timedelta(days=c.audit_interval_days):
                enqueue(c.id, "audit")
            elif any(k.tracked for k in c.keywords) and ranks_due(c):
                enqueue(c.id, "ranks")
            if sources.gsc_ready(c) and (c.gsc_cache or {}).get("fetched") != dt.date.today().isoformat():
                enqueue(c.id, "gsc_sync")  # free, so daily: catches traffic drops between weekly checks
            if ai.enabled():
                primary = locations.all_for(c)[0]["key"]
                for loc in locations.all_for(c):
                    if not locations.gbp_for(c, loc["key"]):
                        continue
                    upcoming = [p for p in c.posts if p.status in ("draft", "approved")
                                and (p.location or primary) == loc["key"]]
                    if len(upcoming) < 3:
                        enqueue(c.id, "gbp_posts", location=loc["key"])


_started = False


def start():
    global _started
    if _started:
        return
    _started = True
    threading.Thread(target=worker_loop, daemon=True, name="seo-worker").start()
    if config.SCHEDULER_ENABLED:
        sched = BackgroundScheduler(daemon=True)
        sched.add_job(tick, "interval", minutes=30, next_run_time=dt.datetime.now() + dt.timedelta(seconds=20))
        sched.start()
