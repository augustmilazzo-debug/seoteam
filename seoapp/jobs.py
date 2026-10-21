"""Background work: the audit pipeline, rank checks, content generation, and
the scheduler that keeps every client re-audited automatically."""

import datetime as dt
import re
import threading
import time
import traceback

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import select

from . import ai, audit, config, crawler, scoring, sources
from .db import Client, GbpPost, Job, Keyword, Page, Recommendation, SessionLocal, now

MAX_PAGE_OPTIMIZATIONS = 6  # Claude page reviews per audit, to control cost
_wake = threading.Event()


# -- queue --------------------------------------------------------------------
def enqueue(client_id: int, kind: str, **params) -> int:
    with SessionLocal() as db:
        existing = db.scalar(select(Job).where(Job.client_id == client_id, Job.kind == kind,
                                               Job.status.in_(["queued", "running"])))
        if existing and kind not in ("draft",):
            return existing.id
        job = Job(client_id=client_id, kind=kind, params=params)
        db.add(job)
        db.commit()
        _wake.set()
        return job.id


class JobLog:
    def __init__(self, db, job: Job):
        self.db, self.job = db, job

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
                job.status = "done"
            except Exception as e:  # keep the worker alive; show the error in the UI
                db.rollback()
                job = db.get(Job, job.id)
                log.job = job
                log(f"FAILED: {e}")
                log(traceback.format_exc()[-3000:])
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
    for k in ("category", "title", "details", "page_url", "keyword", "impact", "effort", "confidence"):
        if k in data:
            setattr(r, k, data[k])
    r.needs_approval = data.get("needs_approval", r.needs_approval or False)
    r.score, r.level = score, level
    r.updated_at = r.last_seen_at = now()
    return r


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
    if not (config.GSC_SERVICE_ACCOUNT_JSON and c.gsc_property):
        log("Search Console not connected for this client (set the property in client settings)")
        return []
    try:
        rows = sources.gsc_rows(c.gsc_property)
    except Exception as e:
        log(f"Search Console error: {e}. Check the service account was added as a user on the property.")
        return []
    rows.sort(key=lambda r: -r["impressions"])
    c.gsc_cache = {"fetched": dt.date.today().isoformat(), "rows": rows[:3000]}
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
                _record_position(k, round(q["pos_w"] / max(1, q["impressions"]), 1), None, q["page"], "gsc")
    db.commit()
    log(f"Search Console: {len(rows)} query/page rows")
    return rows


def _record_position(k: Keyword, pos, local, url, source):
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
    out = []
    for comp in (k.competitors or [])[:3]:
        if not comp.get("url") or own_domain in (comp.get("domain") or ""):
            continue
        try:
            d = crawler.Crawler(comp["url"], max_pages=1, log=lambda *_: None).fetch(crawler.normalize(comp["url"]), 0)
        except Exception:
            continue
        out.append({"rank": comp.get("rank"), "url": comp["url"], "title": d.get("title"), "h1": d.get("h1"),
                    "h2": (d.get("h2") or [])[:20], "word_count": d.get("word_count"), "schema": d.get("schema_types")})
    return out


def _cooling_down(c: Client, url: str) -> bool:
    cutoff = now() - dt.timedelta(days=config.PAGE_COOLDOWN_DAYS)
    return any(r.page_url == url and r.status == "done" and r.done_at and r.done_at > cutoff
               and r.category in ("on_page", "content") for r in c.recommendations)


def step_optimize(db, c: Client, log):
    if not ai.enabled():
        log("Skipping AI page optimization: add ANTHROPIC_API_KEY")
        return
    by_page: dict[str, list[Keyword]] = {}
    for k in c.keywords:
        if k.tracked and k.target_url:
            by_page.setdefault(k.target_url, []).append(k)
    pages = {p.url: p for p in c.pages}

    def urgency(item):
        url, kws = item
        best = max(kws, key=lambda k: k.priority)
        pos = min((k.position for k in kws if k.position), default=None)
        striking = 1.6 if pos and 4 <= pos <= 20 else 1.2 if pos and pos > 20 else 1.0 if pos is None else 0.6
        return sum(k.priority for k in kws) * striking + best.priority

    domain = sources.domain_of(c.url)
    gsc = (c.gsc_cache or {}).get("rows", [])
    site_pages = [f"{p.url} | {p.title}" for p in ranked_pages(c)]
    done = 0
    for url, kws in sorted(by_page.items(), key=urgency, reverse=True):
        if done >= MAX_PAGE_OPTIMIZATIONS:
            break
        p = pages.get(url)
        if not p or p.status_code != 200:
            continue
        if _cooling_down(c, url):
            log(f"Skipping {url}: changed recently, measuring impact (cooldown {config.PAGE_COOLDOWN_DAYS} days)")
            continue
        open_ai = [r for r in c.recommendations if r.page_url == url and r.fingerprint.startswith("ai:page:") and r.status == "open"]
        if open_ai and (p.extra or {}).get("optimized_hash") == p.content_hash:
            continue  # page unchanged and its recommendations are still open
        main = max(kws, key=lambda k: (k.priority, k.search_volume or 0))
        competitors = _competitor_summaries(main, domain, log)
        log(f"Optimizing {url} for {', '.join(k.keyword for k in kws)}")
        recs = ai.optimize_page(c.profile or {}, page_dict(p), [kw_view(k) for k in kws], competitors,
                                [r for r in gsc if r["page"] == url], site_pages)
        for r in open_ai:  # replace stale, unapproved suggestions for this page
            db.delete(r)
        prio = max(k.priority for k in kws)
        for i, r in enumerate(recs):
            cat = r["category"] if r["category"] in ("on_page", "content", "local", "technical") else "on_page"
            upsert_rec(db, c, {**r, "category": cat, "page_url": url, "keyword": r.get("keyword") or main.keyword,
                               "fingerprint": f"ai:page:{url}:{slug(r['title'])}",
                               "needs_approval": cat == "content"}, prio)
        p.extra = {**(p.extra or {}), "optimized_hash": p.content_hash, "optimized_at": dt.date.today().isoformat()}
        db.commit()
        done += 1
    log(f"Page optimization: reviewed {done} page(s)")


def step_new_pages(db, c: Client, log):
    if not ai.enabled():
        return
    covered = {r.keyword.lower() for r in c.recommendations if r.category == "new_page" and r.status != "dismissed"}
    gaps = [k for k in c.keywords if k.tracked and not k.target_url and k.keyword.lower() not in covered]
    if not gaps:
        return
    gaps = sorted(gaps, key=lambda k: -k.priority)[:8]
    log(f"Planning new pages for {len(gaps)} keyword(s) with no page")
    recs = ai.new_page_briefs(c.profile or {}, [kw_view(k) for k in gaps], [f"{p.url} | {p.title}" for p in ranked_pages(c)])
    prio = {k.keyword.lower(): k.priority for k in gaps}
    for r in recs:
        kw = (r.get("keyword") or "").lower()
        upsert_rec(db, c, {**r, "category": "new_page", "fingerprint": f"ai:new:{slug(kw or r['title'])}",
                           "needs_approval": True}, prio.get(kw, 3))
    db.commit()


def step_blog(db, c: Client, log, force=False):
    if not ai.enabled():
        return
    open_blogs = [r for r in c.recommendations if r.category == "blog" and r.status in ("open", "approved")]
    if len(open_blogs) >= 3 and not force:
        return
    blog_titles = [p.title for p in c.pages if re.search(r"/(blog|news|articles?|posts?)/|/\d{4}/\d{2}/", p.url)]
    blog_titles += [r.title for r in c.recommendations if r.category == "blog"]
    log("Generating blog ideas")
    recs = ai.blog_ideas(c.profile or {}, [kw_view(k) for k in c.keywords if k.tracked], blog_titles)
    for r in recs:
        upsert_rec(db, c, {**r, "category": "blog", "fingerprint": f"ai:blog:{slug(r['title'])}", "needs_approval": True},
                   3)
    db.commit()


def step_gbp_audit(db, c: Client, log, force=False):
    if not ai.enabled() or not c.gbp:
        return
    last = (c.gbp or {}).get("_audited")
    if last and not force and (dt.date.today() - dt.date.fromisoformat(last)).days < 28:
        return
    log("Auditing Google Business Profile")
    recs = ai.gbp_audit(c.profile or {}, {k: v for k, v in c.gbp.items() if not k.startswith("_")},
                        [kw_view(k) for k in c.keywords if k.tracked], (c.gbp or {}).get("_maps_competitors", []))
    for r in recs:
        upsert_rec(db, c, {**r, "category": "gbp", "fingerprint": f"ai:gbp:{slug(r['title'])}"}, 4)
    c.gbp = {**c.gbp, "_audited": dt.date.today().isoformat()}
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


def step_ranks(db, c: Client, log):
    tracked = [k for k in c.keywords if k.tracked]
    if not tracked:
        log("No tracked keywords yet")
        return
    if not sources.dfs_enabled():
        log("DataForSEO not connected: positions come from Search Console only")
        step_gsc(db, c, log)
        c.last_rank_at = now()
        db.commit()
        return
    domain = sources.domain_of(c.url)
    name = (c.profile or {}).get("business_name") or c.name
    maps_seen: dict[str, int] = {}
    for k in tracked:
        try:
            org = sources.organic_rank(k.keyword, domain, c.rank_location)
            local = sources.maps_rank(k.keyword, domain, name, c.rank_location)
        except Exception as e:
            log(f"Rank check failed for '{k.keyword}': {e}")
            continue
        _record_position(k, org["position"], local, org["url"], "dataforseo")
        k.competitors = org["top"]
        for title, _ in org.get("local_pack", []):
            if name.lower() not in title.lower():
                maps_seen[title] = maps_seen.get(title, 0) + 1
        log(f"'{k.keyword}': organic {org['position'] or '>100'}, maps {local or '>20'}")
        db.commit()
        time.sleep(0.3)
    c.gbp = {**(c.gbp or {}), "_maps_competitors": sorted(maps_seen, key=lambda t: -maps_seen[t])[:8]}
    c.last_rank_at = now()
    db.commit()


# -- job handlers -------------------------------------------------------------
def job_audit(db, c: Client, log, full=True):
    site = step_crawl(db, c, log)
    if not site.get("home_status"):
        step_technical(db, c, site, log)
        c.last_audit_at = now()
        db.commit()
        return
    step_profile(db, c, log)
    step_gsc(db, c, log)
    step_keywords(db, c, log)
    step_metrics(db, c, log)
    step_map(db, c, log)
    step_pagespeed(db, c, log)
    step_technical(db, c, site, log)
    if any(k.tracked for k in c.keywords) and sources.dfs_enabled() and not c.last_rank_at:
        step_ranks(db, c, log)
    step_optimize(db, c, log)
    step_new_pages(db, c, log)
    step_blog(db, c, log)
    step_gbp_audit(db, c, log)
    step_measure(db, c, log)
    c.last_audit_at = now()
    db.commit()
    log("Audit complete")


def job_ranks(db, c, log):
    step_ranks(db, c, log)
    step_measure(db, c, log)


def job_keywords(db, c, log):
    step_keywords(db, c, log, force=True)


def job_profile(db, c, log):
    step_profile(db, c, log, force=True)


def job_blog(db, c, log):
    step_blog(db, c, log, force=True)


def job_gbp_audit(db, c, log):
    step_gbp_audit(db, c, log, force=True)


def job_gbp_posts(db, c, log, n=8):
    if not ai.enabled():
        raise RuntimeError("Add ANTHROPIC_API_KEY to generate posts")
    recent = [p.title for p in sorted(c.posts, key=lambda p: p.created_at, reverse=True)]
    month = dt.date.today().strftime("for %B")
    posts = ai.gbp_posts(c.profile or {}, [kw_view(k) for k in c.keywords if k.tracked], recent, n, month)
    for p in posts:
        db.add(GbpPost(client_id=c.id, post_type=p["post_type"], title=p["title"], body=p["body"],
                       cta=p["cta"], link=p["link"], keyword=p["keyword"], image_idea=p["image_idea"],
                       suggested_date=dt.date.today() + dt.timedelta(days=max(0, p["days_from_now"]))))
    db.commit()
    log(f"Drafted {len(posts)} posts for approval")


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
}
JOB_LABELS = {
    "audit": "Full site audit", "ranks": "Rank check", "keywords": "Keyword ideas", "profile": "Business profile",
    "blog": "Blog ideas", "gbp_audit": "GBP audit", "gbp_posts": "GBP posts", "draft": "Content draft",
}


# -- scheduler ----------------------------------------------------------------
def tick():
    with SessionLocal() as db:
        for c in db.scalars(select(Client).where(Client.paused.is_(False))):
            if not c.last_audit_at or now() - c.last_audit_at > dt.timedelta(days=c.audit_interval_days):
                enqueue(c.id, "audit")
            elif any(k.tracked for k in c.keywords) and (
                    not c.last_rank_at or now() - c.last_rank_at > dt.timedelta(days=c.rank_interval_days)):
                enqueue(c.id, "ranks")
            if c.gbp and ai.enabled():
                upcoming = [p for p in c.posts if p.status in ("draft", "approved")]
                if len(upcoming) < 3:
                    enqueue(c.id, "gbp_posts")


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
