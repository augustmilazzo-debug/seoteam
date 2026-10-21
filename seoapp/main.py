"""Web dashboard. Run locally with:  uvicorn seoapp.main:app --reload"""

import datetime as dt
import hmac
from html import escape as html_escape
from urllib.parse import quote
from contextlib import asynccontextmanager

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markdown_it import MarkdownIt
from sqlalchemy import select
from itsdangerous import BadSignature, URLSafeTimedSerializer
from starlette.middleware.sessions import SessionMiddleware

from . import audit, blog, config, crawler, jobs, locations, overlap, sources
from .db import BlogPost, Client, GbpPost, Job, Keyword, Page, Recommendation, SessionLocal, init_db, now


@asynccontextmanager
async def lifespan(app):
    init_db()
    jobs.start()
    yield


app = FastAPI(title="SEO Command Center", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=config.ROOT / "static"), name="static")
templates = Jinja2Templates(directory=config.ROOT / "templates")
md = MarkdownIt("commonmark", {"html": False}).enable("table")

CATEGORIES = {
    "technical": "Technical", "on_page": "On-page", "content": "Content", "new_page": "New page",
    "blog": "Blog", "gbp": "Google Business Profile", "local": "Local",
}
LEVEL_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def ago(d):
    if not d:
        return "never"
    s = (now() - d).total_seconds()
    if s < 0:
        return "just now"
    for unit, n in (("day", 86400), ("hour", 3600), ("minute", 60)):
        if s >= n:
            v = int(s // n)
            return f"{v} {unit}{'s' if v != 1 else ''} ago"
    return "just now"


templates.env.filters["md"] = lambda s: md.render(s or "")
templates.env.filters["ago"] = ago
templates.env.globals.update(CATEGORIES=CATEGORIES, JOB_LABELS=jobs.JOB_LABELS, integrations=config.integrations,
                             RANK_DEPTH=sources.RANK_DEPTH)


# -- auth ---------------------------------------------------------------------
@app.middleware("http")
async def require_login(request: Request, call_next):
    open_paths = ("/login", "/static", "/healthz")
    if config.APP_PASSWORD and not request.url.path.startswith(open_paths) \
            and not request.session.get("auth"):
        return RedirectResponse("/login", status_code=303)
    return await call_next(request)


# Added after require_login so it wraps it: the session must exist before the login check reads it.
app.add_middleware(SessionMiddleware, secret_key=config.SECRET_KEY, max_age=60 * 60 * 24 * 30, https_only=False)


@app.get("/healthz")
def healthz():
    return PlainTextResponse("ok")


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": ""})


@app.post("/login")
def login(request: Request, password: str = Form(...)):
    if config.APP_PASSWORD and hmac.compare_digest(password, config.APP_PASSWORD):
        request.session["auth"] = True
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"error": "Wrong password"}, status_code=401)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# -- helpers ------------------------------------------------------------------
def get_or_404(db, model, oid: int):
    obj = db.get(model, oid)
    if not obj:
        raise HTTPException(404, "Not found")
    return obj


def get_client(db, cid: int) -> Client:
    return get_or_404(db, Client, cid)


def open_recs(c: Client):
    return sorted([r for r in c.recommendations if r.status in ("open", "approved")],
                  key=lambda r: (-r.score, LEVEL_ORDER.get(r.level, 9)))


def client_stats(c: Client) -> dict:
    recs = open_recs(c)
    tracked = [k for k in c.keywords if k.tracked]
    ranked = [k.position for k in tracked if k.position]
    return {
        "health": audit.health_score(c.recommendations),
        "open": len(recs),
        "critical": sum(r.level == "critical" for r in recs),
        "high": sum(r.level == "high" for r in recs),
        "tracked": len(tracked),
        "top3": sum(1 for p in ranked if p <= 3),
        "top10": sum(1 for p in ranked if p <= 10),
        "avg_pos": round(sum(ranked) / len(ranked), 1) if ranked else None,
        "maps_top3": sum(1 for k in tracked if k.local_pack_position and k.local_pack_position <= 3),
        "pages": len(jobs.html_pages(c)),
        "awaiting_approval": sum(1 for r in c.recommendations if r.draft_status == "draft")
        + sum(1 for p in c.posts if p.status == "draft") + sum(1 for b in c.blog_posts if b.status == "review"),
        "running": next((j for j in sorted(c.jobs, key=lambda j: -j.id) if j.status in ("queued", "running")), None),
        "last_job": next((j for j in sorted(c.jobs, key=lambda j: -j.id)), None),
    }


def job_error(j) -> str:
    """The plain-English reason a job failed (its FAILED: line)."""
    for line in reversed((j.log or "").splitlines()):
        if "FAILED:" in line:
            return line.split("FAILED:", 1)[1].strip()[:400]
    return ""


templates.env.filters["job_error"] = job_error


def render(request, name, **ctx):
    return templates.TemplateResponse(request, name, ctx)


def back(request: Request, default: str):
    ref = request.headers.get("referer") or default
    return RedirectResponse(ref, status_code=303)


# -- dashboard ----------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    with SessionLocal() as db:
        clients = db.scalars(select(Client).order_by(Client.name)).all()
        rows = [(c, client_stats(c)) for c in clients]
        top = []
        for c in clients:
            top += [(c, r) for r in open_recs(c)[:5]]
        top.sort(key=lambda cr: -cr[1].score)
        return render(request, "dashboard.html", rows=rows, top=top[:12])


@app.post("/clients")
def add_client(url: str = Form(...), name: str = Form(""), cms: str = Form("unknown"),
               gsc_property: str = Form("")):
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    with SessionLocal() as db:
        c = Client(name=name.strip() or sources.domain_of(url), url=crawler.normalize(url), cms=cms,
                   gsc_property=gsc_property.strip())
        db.add(c)
        db.commit()
        cid = c.id
    jobs.enqueue(cid, "audit")
    return RedirectResponse(f"/clients/{cid}", status_code=303)


# -- client pages -------------------------------------------------------------
@app.get("/clients/{cid}", response_class=HTMLResponse)
def client_overview(request: Request, cid: int):
    with SessionLocal() as db:
        c = get_client(db, cid)
        tracked = sorted([k for k in c.keywords if k.tracked], key=lambda k: (-k.priority, k.position or 999))
        recent_done = sorted([r for r in c.recommendations if r.status in ("done", "resolved")],
                             key=lambda r: r.done_at or r.updated_at, reverse=True)[:8]
        rows = page_rows(c)
        dist = [(label, cls, sum(r["status"] == k for r in rows), k) for k, label, cls in PAGE_STATUS]
        alerts = sorted(c.alerts, key=lambda a: a.created_at, reverse=True)[:12]
        page_ids = {p.url: p.id for p in c.pages}
        return render(request, "client_overview.html", c=c, s=client_stats(c), tab="overview",
                      recs=open_recs(c)[:10], tracked=tracked[:15], recent_done=recent_done, dist=dist,
                      alerts=alerts, page_ids=page_ids)


@app.get("/clients/{cid}/recommendations", response_class=HTMLResponse)
def client_recs(request: Request, cid: int, category: str = "", status: str = "active", level: str = ""):
    with SessionLocal() as db:
        c = get_client(db, cid)
        recs = c.recommendations
        if status == "active":
            recs = [r for r in recs if r.status in ("open", "approved")]
        elif status:
            recs = [r for r in recs if r.status == status]
        if category:
            recs = [r for r in recs if r.category == category]
        if level:
            recs = [r for r in recs if r.level == level]
        recs = sorted(recs, key=lambda r: (-r.score, LEVEL_ORDER.get(r.level, 9)))
        counts = {}
        for r in c.recommendations:
            if r.status in ("open", "approved"):
                counts[r.category] = counts.get(r.category, 0) + 1
        return render(request, "client_recs.html", c=c, s=client_stats(c), tab="recs", recs=recs,
                      f={"category": category, "status": status, "level": level}, counts=counts)


@app.get("/recommendations/{rid}", response_class=HTMLResponse)
def rec_detail(request: Request, rid: int):
    with SessionLocal() as db:
        r = get_or_404(db, Recommendation, rid)
        c = r.client
        kw = next((k for k in c.keywords if k.keyword.lower() == (r.keyword or "").lower()), None)
        draft_job = next((j for j in sorted(c.jobs, key=lambda j: -j.id)
                          if j.kind == "draft" and (j.params or {}).get("rec_id") == r.id), None)
        return render(request, "rec_detail.html", c=c, s=client_stats(c), tab="recs", r=r, kw=kw, draft_job=draft_job)


@app.post("/recommendations/{rid}/status")
def rec_status(request: Request, rid: int, status: str = Form(...)):
    if status not in ("open", "approved", "done", "dismissed"):
        raise HTTPException(400)
    with SessionLocal() as db:
        r = get_or_404(db, Recommendation, rid)
        r.status = status
        r.updated_at = now()
        if status == "done":
            r.done_at = now()
            kw = next((k for k in r.client.keywords if k.keyword.lower() == (r.keyword or "").lower()), None)
            r.baseline = {"position": kw.position if kw else None, "maps": kw.local_pack_position if kw else None,
                          "date": dt.date.today().isoformat()}
        db.commit()
    return back(request, f"/recommendations/{rid}")


@app.post("/recommendations/{rid}/draft")
def rec_draft(rid: int, feedback: str = Form("")):
    with SessionLocal() as db:
        r = get_or_404(db, Recommendation, rid)
        cid = r.client_id
        r.draft_status = "writing" if not r.draft else r.draft_status
        db.commit()
    jobs.enqueue(cid, "draft", rec_id=rid, feedback=feedback)
    return RedirectResponse(f"/recommendations/{rid}", status_code=303)


@app.post("/recommendations/{rid}/draft/save")
def rec_draft_save(rid: int, draft: str = Form(...), action: str = Form("save")):
    with SessionLocal() as db:
        r = get_or_404(db, Recommendation, rid)
        r.draft = draft
        if action == "approve":
            r.draft_status = "approved"
            if r.status == "open":
                r.status = "approved"
        elif action == "reject":
            r.draft_status = "rejected"
        db.commit()
    return RedirectResponse(f"/recommendations/{rid}", status_code=303)


@app.get("/clients/{cid}/keywords", response_class=HTMLResponse)
def client_keywords(request: Request, cid: int, show: str = "all"):
    with SessionLocal() as db:
        c = get_client(db, cid)
        kws = c.keywords
        if show == "tracked":
            kws = [k for k in kws if k.tracked]
        elif show == "ideas":
            kws = [k for k in kws if not k.tracked]
        kws = sorted(kws, key=lambda k: (not k.tracked, -k.priority, -(k.search_volume or 0), k.keyword))
        pages = [p.url for p in jobs.ranked_pages(c)]
        gsc_ready = sources.gsc_ready(c)
        return render(request, "client_keywords.html", c=c, s=client_stats(c), tab="keywords", kws=kws,
                      pages=pages, show=show, dfs_ready=sources.dfs_enabled(), gsc_ready=gsc_ready)


@app.post("/clients/{cid}/keywords/add")
def keywords_add(cid: int, keywords: str = Form(...), priority: int = Form(4)):
    with SessionLocal() as db:
        c = get_client(db, cid)
        have = {k.keyword.lower() for k in c.keywords}
        added = []
        for line in keywords.splitlines():
            kw = line.strip().lower()
            if kw and kw not in have:
                have.add(kw)
                k = Keyword(client_id=c.id, keyword=kw, source="manual", priority=max(1, min(5, priority)), tracked=True)
                db.add(k)
                added.append(k)
        db.commit()
        if added:
            jobs.step_metrics(db, c, lambda *_: None, added)
    return RedirectResponse(f"/clients/{cid}/keywords", status_code=303)


@app.post("/keywords/{kid}")
def keyword_update(request: Request, kid: int, priority: int = Form(...), target_url: str = Form(""),
                   tracked: str = Form("")):
    with SessionLocal() as db:
        k = get_or_404(db, Keyword, kid)
        k.priority = max(1, min(5, priority))
        k.target_url = target_url.strip()
        k.tracked = tracked == "on"
        db.commit()
    return back(request, "/")


@app.post("/clients/{cid}/keywords/bulk")
def keywords_bulk(cid: int, action: str = Form(...), ids: list[int] = Form(default=[])):
    with SessionLocal() as db:
        for kid in ids:
            k = db.get(Keyword, kid)
            if not k or k.client_id != cid:
                continue
            if action == "track":
                k.tracked = True
            elif action == "untrack":
                k.tracked = False
            elif action == "delete":
                db.delete(k)
        db.commit()
    return RedirectResponse(f"/clients/{cid}/keywords", status_code=303)


PAGE_STATUS = [  # key, label, badge class
    ("top3", "Top 3", "b-ok"), ("page1", "Page 1", "b-ok"), ("page2", "Page 2", "b-medium"),
    ("page3", "Page 3", "b-high"), ("not_ranking", f"Not in top {sources.RANK_DEPTH}", "b-critical"),
    ("unchecked", "Not checked yet", "b-gray"), ("no_target", "No target keyword", "b-gray"),
]
STATUS_INFO = {k: (label, cls) for k, label, cls in PAGE_STATUS}


def change_since(k, days: int):
    """Positions gained (+) or lost (-) versus the latest check at least `days` ago."""
    if not k or k.position is None:
        return None
    cutoff = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    older = [h for h in (k.history or [])[:-1] if h["date"] <= cutoff and h.get("position") is not None]
    return round(older[-1]["position"] - k.position, 1) if older else None


def page_rows(c: Client) -> list[dict]:
    by_page = jobs.page_keywords(c)
    open_by_page: dict[str, int] = {}
    for r in c.recommendations:
        if r.status in ("open", "approved") and r.page_url:
            open_by_page[r.page_url] = open_by_page.get(r.page_url, 0) + 1
    rows = []
    for p in jobs.ranked_pages(c):
        kws = by_page.get(p.url, [])
        main = kws[0] if kws else None
        if not main:
            status = "no_target"
        elif main.last_checked is None:
            status = "unchecked"
        elif main.position is None:
            status = "not_ranking"
        else:
            status = "top3" if main.position <= 3 else f"page{min(3, jobs.google_page(main.position))}"
        rows.append({"page": p, "kws": kws, "main": main, "status": status,
                     "week": change_since(main, 6), "month": change_since(main, 27),
                     "open": open_by_page.get(p.url, 0), "extra": p.extra or {}})
    order = {k: i for i, (k, _, _) in enumerate(PAGE_STATUS)}
    rows.sort(key=lambda r: (order[r["status"]], r["main"].position if r["main"] and r["main"].position else 999))
    return rows


@app.get("/clients/{cid}/pages", response_class=HTMLResponse)
def client_pages(request: Request, cid: int, status: str = ""):
    with SessionLocal() as db:
        c = get_client(db, cid)
        rows = page_rows(c)
        counts = {k: sum(r["status"] == k for r in rows) for k, _, _ in PAGE_STATUS}
        if status:
            rows = [r for r in rows if r["status"] == status]
        gsc_ready = sources.gsc_ready(c)
        return render(request, "client_pages.html", c=c, s=client_stats(c), tab="pages", rows=rows,
                      counts=counts, statuses=PAGE_STATUS, info=STATUS_INFO, f_status=status,
                      rank_ready=sources.dfs_enabled() or gsc_ready)


@app.get("/clients/{cid}/urls", response_class=HTMLResponse)
def client_urls(request: Request, cid: int):
    with SessionLocal() as db:
        c = get_client(db, cid)
        pages = sorted(c.pages, key=lambda p: (p.status_code != 200, p.depth, p.url))
        targets = {}
        for k in c.keywords:
            if k.tracked and k.target_url:
                targets.setdefault(k.target_url, []).append(k.keyword)
        return render(request, "client_urls.html", c=c, s=client_stats(c), tab="pages", pages=pages, targets=targets)


@app.get("/pages/{pid}", response_class=HTMLResponse)
def page_detail(request: Request, pid: int):
    with SessionLocal() as db:
        p = get_or_404(db, Page, pid)
        c = p.client
        row = next((r for r in page_rows(c) if r["page"].id == p.id), None)
        recs = [r for r in open_recs(c) if r.page_url == p.url]
        alerts = sorted([a for a in c.alerts if a.page_url == p.url], key=lambda a: a.created_at, reverse=True)[:15]
        review_job = next((j for j in sorted(c.jobs, key=lambda j: -j.id)
                           if j.kind == "review_page" and (j.params or {}).get("page_id") == p.id), None)
        rank_job = next((j for j in sorted(c.jobs, key=lambda j: -j.id)
                         if j.kind == "ranks" and (j.params or {}).get("page_url") == p.url), None)
        return render(request, "page_detail.html", c=c, s=client_stats(c), tab="pages", p=p, row=row, recs=recs,
                      alerts=alerts, info=STATUS_INFO, review_job=review_job, rank_job=rank_job,
                      dfs_ready=sources.dfs_enabled())


@app.post("/pages/{pid}/review")
def page_review(pid: int):
    with SessionLocal() as db:
        p = get_or_404(db, Page, pid)
        cid = p.client_id
    jobs.enqueue(cid, "review_page", page_id=pid)
    return RedirectResponse(f"/pages/{pid}", status_code=303)


@app.post("/pages/{pid}/ranks")
def page_ranks(pid: int):
    with SessionLocal() as db:
        p = get_or_404(db, Page, pid)
        cid, url = p.client_id, p.url
    jobs.enqueue(cid, "ranks", page_url=url)
    return RedirectResponse(f"/pages/{pid}", status_code=303)


@app.post("/pages/{pid}/keywords")
def page_add_keyword(pid: int, keyword: str = Form(...), role: str = Form("secondary"), priority: int = Form(4)):
    kw = keyword.strip().lower()
    with SessionLocal() as db:
        p = get_or_404(db, Page, pid)
        c = p.client
        if kw:
            if role == "primary":
                for k in c.keywords:
                    if k.target_url == p.url and k.role == "primary":
                        k.role = "secondary"
            k = next((k for k in c.keywords if k.keyword.lower() == kw), None)
            if k is None:
                k = Keyword(client_id=c.id, keyword=kw, source="manual")
                db.add(k)
            k.target_url, k.role, k.tracked = p.url, role if role in ("primary", "secondary") else "secondary", True
            k.priority = max(1, min(5, priority))
            db.commit()
            jobs.step_metrics(db, c, lambda *_: None, [k])
    return RedirectResponse(f"/pages/{pid}", status_code=303)


BLOG_STAGES = [  # status, label, badge
    ("review", "Ready for your review", "b-high"), ("writing", "Being written", "b-brand"),
    ("idea", "Ideas", "b-gray"), ("approved", "Approved", "b-brand"), ("synced", "Draft in WordPress", "b-ok"),
    ("published", "Published", "b-ok"), ("rejected", "Rejected", "b-gray"),
]


@app.get("/clients/{cid}/blog", response_class=HTMLResponse)
def client_blog(request: Request, cid: int):
    with SessionLocal() as db:
        c = get_client(db, cid)
        groups = []
        for st, label, cls in BLOG_STAGES:
            posts = sorted([b for b in c.blog_posts if b.status == st],
                           key=lambda b: (-b.priority, -(b.search_volume or 0), -b.id))
            groups.append((st, label, cls, posts))
        pages = [p.url for p in jobs.ranked_pages(c)]
        locs = {l["key"]: l["label"] for l in locations.all_for(c)}
        return render(request, "client_blog.html", c=c, s=client_stats(c), tab="blog", groups=groups, pages=pages,
                      locs=locs)


@app.post("/clients/{cid}/blog/new")
def blog_new(cid: int, title: str = Form(...), target_keyword: str = Form(""), notes: str = Form(""),
             supports_url: str = Form(""), write_now: str = Form(""), location: str = Form("")):
    with SessionLocal() as db:
        c = get_client(db, cid)
        b = BlogPost(client_id=c.id, source="manual", title=title.strip(), target_keyword=target_keyword.strip().lower(),
                     angle=notes.strip(), supports_url=supports_url.strip(), priority=4, location=location)
        db.add(b)
        db.commit()
        bid = b.id
    if write_now:
        jobs.enqueue(cid, "blog_write", post_id=bid)
    return RedirectResponse(f"/blog/{bid}", status_code=303)


@app.get("/blog/{bid}", response_class=HTMLResponse)
def blog_detail(request: Request, bid: int):
    with SessionLocal() as db:
        b = get_or_404(db, BlogPost, bid)
        c = b.client
        job = next((j for j in sorted(c.jobs, key=lambda j: -j.id)
                    if j.kind == "blog_write" and (j.params or {}).get("post_id") == b.id), None)
        ctx = blog.site_context(c)
        checks = blog.checklist(b, sources.domain_of(c.url), c.profile or {}, ctx) if b.body else []
        preview = blog.full_html(b, c.profile or {}, ctx, include_schema=False) if b.body else ""
        schema = blog.schema_json(b, c.profile or {}, ctx) if b.body else ""
        faq_text = "\n\n".join(f"Q: {f['question']}\nA: {f['answer']}" for f in b.faqs or [])
        loc_label = locations.get(c, b.location)["label"] if b.location else ""
        similar = overlap.matches(c, b.target_keyword or b.title, b.title,
                                  items=overlap.inventory(c, exclude_post_id=b.id))[:6]
        return render(request, "blog_post.html", c=c, s=client_stats(c), tab="blog", b=b, job=job, checks=checks,
                      loc_label=loc_label, similar=similar, DUPLICATE=overlap.DUPLICATE,
                      preview=preview, faq_text=faq_text, schema=schema, ctx=ctx, stages={st: (label, cls) for st, label, cls in BLOG_STAGES},
                      msg=request.query_params.get("msg", ""), err=request.query_params.get("err", ""))


@app.post("/blog/{bid}/write")
def blog_write(bid: int, feedback: str = Form("")):
    with SessionLocal() as db:
        b = get_or_404(db, BlogPost, bid)
        b.feedback = feedback.strip()
        db.commit()
        cid = b.client_id
    jobs.enqueue(cid, "blog_write", post_id=bid, rev=feedback.strip()[:100])
    return RedirectResponse(f"/blog/{bid}", status_code=303)


def _parse_faqs(text_: str) -> list[dict]:
    faqs, q = [], None
    for line in (text_ or "").splitlines():
        line = line.strip()
        if line[:2].upper() == "Q:":
            q = line[2:].strip()
        elif line[:2].upper() == "A:" and q:
            faqs.append({"question": q, "answer": line[2:].strip()})
            q = None
        elif line and faqs and q is None:
            faqs[-1]["answer"] += " " + line
    return faqs


@app.post("/blog/{bid}/save")
def blog_save(bid: int, title: str = Form(...), meta_title: str = Form(""), meta_description: str = Form(""),
              slug: str = Form(""), target_keyword: str = Form(""), answer_summary: str = Form(""),
              body: str = Form(""), faqs: str = Form(""), action: str = Form("save")):
    with SessionLocal() as db:
        b = get_or_404(db, BlogPost, bid)
        b.title, b.meta_title, b.meta_description = title.strip(), meta_title.strip(), meta_description.strip()
        b.slug, b.target_keyword, b.answer_summary = jobs.slug(slug or title), target_keyword.strip().lower(), answer_summary.strip()
        if body.strip():
            b.body = body
        b.faqs = _parse_faqs(faqs)
        if action == "approve":
            b.status = "approved"
        b.updated_at = now()
        db.commit()
    return RedirectResponse(f"/blog/{bid}?msg=" + ("Approved" if action == "approve" else "Saved"), status_code=303)


@app.post("/blog/{bid}/status")
def blog_status(request: Request, bid: int, status: str = Form(...)):
    if status not in ("idea", "review", "approved", "published", "rejected"):
        raise HTTPException(400)
    with SessionLocal() as db:
        b = get_or_404(db, BlogPost, bid)
        b.status, b.updated_at = status, now()
        db.commit()
    return back(request, f"/blog/{bid}")


@app.post("/blog/{bid}/sync")
def blog_sync(bid: int):
    with SessionLocal() as db:
        b = get_or_404(db, BlogPost, bid)
        c = b.client
        try:
            b.cms_id, b.cms_edit_url, b.cms_check = blog.wp_push(c.wp or {}, b, c.profile or {}, blog.site_context(c))
        except blog.WordPressError as e:
            return RedirectResponse(f"/blog/{bid}?err=" + quote(str(e)), status_code=303)
        b.status, b.synced_at, b.updated_at = "synced", now(), now()
        db.commit()
    return RedirectResponse(f"/blog/{bid}?msg=" + quote("Sent to WordPress as a draft"), status_code=303)


@app.get("/blog/{bid}/export.html")
def blog_export(bid: int):
    with SessionLocal() as db:
        b = get_or_404(db, BlogPost, bid)
        page = (f"<!doctype html><html><head><meta charset='utf-8'><title>{html_escape(b.meta_title or b.title)}</title>"
                f"<meta name='description' content='{html_escape(b.meta_description)}'></head><body>"
                f"<h1>{html_escape(b.title)}</h1>{blog.full_html(b, b.client.profile or {}, blog.site_context(b.client))}</body></html>")
        return HTMLResponse(page, headers={"Content-Disposition": f'attachment; filename="{b.slug or "post"}.html"'})


# -- Google Search Console sign-in (per client) ------------------------------
def _google_redirect_uri(request: Request) -> str:
    base = config.APP_BASE_URL or str(request.base_url).rstrip("/")
    return f"{base}/oauth/google/callback"


@app.get("/clients/{cid}/gsc/connect")
def gsc_connect(request: Request, cid: int):
    if not sources.google_oauth_ready():
        return RedirectResponse(f"/clients/{cid}/settings?gsc_err=" + quote(
            "Google sign-in isn't set up yet: add GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in Render (see Settings).")
            + "#gsc", status_code=303)
    state = URLSafeTimedSerializer(config.SECRET_KEY, salt="gsc").dumps({"cid": cid})
    return RedirectResponse(sources.google_auth_url(state, _google_redirect_uri(request)), status_code=303)


@app.get("/oauth/google/callback")
def gsc_callback(request: Request, state: str = "", code: str = "", error: str = ""):
    try:
        cid = URLSafeTimedSerializer(config.SECRET_KEY, salt="gsc").loads(state, max_age=900)["cid"]
    except (BadSignature, KeyError, TypeError):
        raise HTTPException(400, "This sign-in link expired. Start again from the client's settings.")
    if error or not code:
        return RedirectResponse(f"/clients/{cid}/settings?gsc_err=" + quote(f"Google sign-in was cancelled ({error or 'no code'}).")
                                + "#gsc", status_code=303)
    try:
        auth = sources.google_exchange(code, _google_redirect_uri(request))
    except sources.GSCError as e:
        return RedirectResponse(f"/clients/{cid}/settings?gsc_err=" + quote(str(e)) + "#gsc", status_code=303)
    with SessionLocal() as db:
        c = get_client(db, cid)
        c.gsc_auth = auth
        db.commit()
    return RedirectResponse(f"/clients/{cid}/settings?gsc_msg=" + quote(
        f"Connected as {auth['email'] or 'your Google account'}. Now choose the property below.") + "#gsc", status_code=303)


@app.post("/clients/{cid}/gsc")
def gsc_save(cid: int, gsc_property: str = Form(""), action: str = Form("save")):
    with SessionLocal() as db:
        c = get_client(db, cid)
        if action == "disconnect":
            c.gsc_auth, c.gsc_property = {}, ""
            db.commit()
            return RedirectResponse(f"/clients/{cid}/settings?gsc_msg=Disconnected#gsc", status_code=303)
        c.gsc_property = gsc_property.strip()
        db.commit()
        ok, msg = sources.gsc_test(c)
    if ok:
        jobs.enqueue(cid, "gsc_sync")
    key = "gsc_msg" if ok else "gsc_err"
    return RedirectResponse(f"/clients/{cid}/settings?{key}=" + quote(msg) + "#gsc", status_code=303)


@app.post("/clients/{cid}/wordpress")
def client_wordpress(cid: int, wp_url: str = Form(""), wp_user: str = Form(""), wp_app_password: str = Form(""),
                     action: str = Form("save")):
    with SessionLocal() as db:
        c = get_client(db, cid)
        wp = dict(c.wp or {})
        wp["url"], wp["user"] = wp_url.strip(), wp_user.strip()
        if wp_app_password.strip():
            wp["app_password"] = wp_app_password.strip()
        if action == "disconnect":
            wp = {}
        c.wp = wp
        db.commit()
        msg = "WordPress settings saved"
        if action == "test" and wp:
            try:
                msg = blog.wp_test(wp)
            except blog.WordPressError as e:
                return RedirectResponse(f"/clients/{cid}/settings?wp_err=" + quote(str(e)) + "#wordpress", status_code=303)
    return RedirectResponse(f"/clients/{cid}/settings?wp_msg=" + quote(msg) + "#wordpress", status_code=303)


GBP_FIELDS = [
    ("profile_url", "Google Maps / profile link", "text"),
    ("primary_category", "Primary category", "text"),
    ("secondary_categories", "Secondary categories (comma separated)", "text"),
    ("business_name", "Business name exactly as shown on Google", "text"),
    ("address", "Address on the profile", "text"),
    ("phone", "Phone on the profile", "text"),
    ("website_link", "Website link on the profile", "text"),
    ("booking_link", "Booking link", "text"),
    ("hours", "Hours", "text"),
    ("description", "Business description", "textarea"),
    ("services", "Services listed (one per line)", "textarea"),
    ("review_count", "Number of reviews", "text"),
    ("rating", "Star rating", "text"),
    ("reviews_last_30_days", "New reviews in the last 30 days", "text"),
    ("review_response_rate", "Share of reviews you reply to (e.g. 50%)", "text"),
    ("photo_count", "Number of photos", "text"),
    ("posts_last_30_days", "Posts in the last 30 days", "text"),
    ("qna_count", "Questions & answers on the profile", "text"),
    ("attributes", "Attributes set (e.g. women-owned, wheelchair accessible)", "text"),
    ("notes", "Anything else (competitors, issues, suspensions)", "textarea"),
]


@app.get("/clients/{cid}/gbp", response_class=HTMLResponse)
def client_gbp(request: Request, cid: int, loc: str = ""):
    with SessionLocal() as db:
        c = get_client(db, cid)
        locs = locations.all_for(c)
        cur = locations.get(c, loc)
        primary = locs[0]["key"]
        mine = lambda key: (key or primary) == cur["key"]
        posts = sorted([p for p in c.posts if mine(p.location)],
                       key=lambda p: ({"draft": 0, "approved": 1, "published": 2, "rejected": 3}[p.status],
                                      p.suggested_date or dt.date.max))
        recs = [r for r in open_recs(c) if r.category in ("gbp", "local") and mine(r.location)]
        kws = []
        for k in sorted([k for k in c.keywords if k.tracked], key=lambda k: -k.priority):
            if any(l["key"] == cur["key"] for l in locations.for_keyword(c, k)):
                r = (k.loc_ranks or {}).get(cur["key"], {})
                kws.append({"keyword": k.keyword, "priority": k.priority, "checked": bool(k.last_checked),
                            "maps": r.get("maps") if r else k.local_pack_position,
                            "position": r.get("position") if r else k.position})
        counts = {l["key"]: sum(1 for p in c.posts if p.status == "draft" and (p.location or primary) == l["key"])
                  for l in locs}
        return render(request, "client_gbp.html", c=c, s=client_stats(c), tab="gbp", posts=posts, fields=GBP_FIELDS,
                      gbp=locations.gbp_for(c, cur["key"]), recs=recs, kws=kws, locs=locs, cur=cur, counts=counts)


@app.post("/clients/{cid}/gbp")
async def client_gbp_save(request: Request, cid: int, loc: str = ""):
    form = await request.form()
    with SessionLocal() as db:
        c = get_client(db, cid)
        key = locations.get(c, loc)["key"]
        data = {k: v for k, v in locations.gbp_for(c, key).items() if k.startswith("_") and k != "_audited"}
        for field, _, _ in GBP_FIELDS:
            v = str(form.get(field, "")).strip()
            if v:
                data[field] = v
        locations.set_gbp(c, key, data)  # changed: audit it again
        db.commit()
    jobs.enqueue(cid, "gbp_audit", location=key)
    return RedirectResponse(f"/clients/{cid}/gbp?loc={key}", status_code=303)


@app.post("/clients/{cid}/gbp/posts")
def client_gbp_posts(cid: int, loc: str = ""):
    with SessionLocal() as db:
        key = locations.get(get_client(db, cid), loc)["key"]
    jobs.enqueue(cid, "gbp_posts", location=key)
    return RedirectResponse(f"/clients/{cid}/gbp?loc={key}", status_code=303)


@app.post("/posts/{pid}")
def post_update(request: Request, pid: int, action: str = Form(...), title: str = Form(None),
                body: str = Form(None), link: str = Form(None), suggested_date: str = Form(None)):
    with SessionLocal() as db:
        p = get_or_404(db, GbpPost, pid)
        if title is not None:
            p.title, p.body, p.link = title, body, link
        if suggested_date:
            try:
                p.suggested_date = dt.date.fromisoformat(suggested_date)
            except ValueError:
                pass
        if action in ("approved", "published", "rejected", "draft"):
            p.status = action
        elif action == "delete":
            db.delete(p)
        db.commit()
    return back(request, "/")


@app.get("/clients/{cid}/settings", response_class=HTMLResponse)
def client_settings(request: Request, cid: int):
    with SessionLocal() as db:
        c = get_client(db, cid)
        sites, sites_err = [], ""
        if (c.gsc_auth or {}).get("refresh_token") or config.GSC_SERVICE_ACCOUNT_JSON:
            try:
                sites = sources.gsc_sites(c)
            except sources.GSCError as e:
                sites_err = str(e)
        return render(request, "client_settings.html", c=c, s=client_stats(c), tab="settings",
                      p=c.profile or {}, gsc_email=sources.gsc_service_email(), gsc_sites=sites, gsc_sites_err=sites_err,
                      oauth_ready=sources.google_oauth_ready())


def _lines(s: str) -> list[str]:
    return [x.strip() for x in (s or "").splitlines() if x.strip()]


@app.post("/clients/{cid}/settings")
def client_settings_save(cid: int, name: str = Form(...), url: str = Form(...), cms: str = Form("unknown"),
                         rank_location: str = Form(""),
                         audit_interval_days: int = Form(7), rank_interval_days: int = Form(3),
                         paused: str = Form(""), notes: str = Form("")):
    with SessionLocal() as db:
        c = get_client(db, cid)
        c.name, c.url, c.cms = name.strip(), crawler.normalize(url if "://" in url else "https://" + url), cms
        c.rank_location = rank_location.strip()
        c.audit_interval_days = max(1, min(60, audit_interval_days))
        c.rank_interval_days = max(1, min(30, rank_interval_days))
        c.paused, c.notes = paused == "on", notes
        db.commit()
    return RedirectResponse(f"/clients/{cid}/settings?saved=1", status_code=303)


@app.post("/clients/{cid}/profile")
def client_profile_save(cid: int, business_name: str = Form(""), business_type: str = Form(""),
                        is_medical: str = Form(""), summary: str = Form(""), services: str = Form(""),
                        locations_text: str = Form("", alias="locations"), service_areas: str = Form(""), usps: str = Form(""),
                        providers: str = Form(""), brand_voice: str = Form(""), booking_url: str = Form("")):
    with SessionLocal() as db:
        c = get_client(db, cid)
        old = {s.get("name"): s for s in (c.profile or {}).get("services", [])}
        svc = []
        for line in _lines(services):
            name, _, url = [x.strip() for x in line.partition("|")]
            svc.append({"name": name, "url": url or old.get(name, {}).get("url", ""),
                        "description": old.get(name, {}).get("description", "")})
        locs = []
        for line in _lines(locations_text):
            parts = [x.strip() for x in line.split("|")] + ["", "", "", "", ""]
            locs.append({"name": parts[0], "address": parts[1], "phone": parts[2], "dataforseo_location": parts[3],
                         "city": parts[0], "state": "", "service_areas": [a.strip() for a in parts[4].split(",") if a.strip()]})
        c.profile = {**(c.profile or {}), "business_name": business_name, "business_type": business_type,
                     "is_medical": is_medical == "on", "summary": summary, "services": svc, "locations": locs,
                     "service_areas": _lines(service_areas), "unique_selling_points": _lines(usps),
                     "providers": _lines(providers), "brand_voice": brand_voice, "booking_url": booking_url}
        db.commit()
    return RedirectResponse(f"/clients/{cid}/settings?saved=1", status_code=303)


@app.post("/clients/{cid}/run/{kind}")
def client_run(request: Request, cid: int, kind: str):
    if kind not in jobs.HANDLERS or kind in ("draft", "review_page"):
        raise HTTPException(400)
    jobs.enqueue(cid, kind)
    return back(request, f"/clients/{cid}")


@app.post("/clients/{cid}/delete")
def client_delete(cid: int, confirm: str = Form("")):
    with SessionLocal() as db:
        c = get_client(db, cid)
        if confirm.strip().lower() != c.name.strip().lower():
            return RedirectResponse(f"/clients/{cid}/settings?delete_error=1", status_code=303)
        db.delete(c)
        db.commit()
    return RedirectResponse("/", status_code=303)


@app.get("/clients/{cid}/activity", response_class=HTMLResponse)
def client_activity(request: Request, cid: int):
    with SessionLocal() as db:
        c = get_client(db, cid)
        js = sorted(c.jobs, key=lambda j: -j.id)[:40]
        return render(request, "client_activity.html", c=c, s=client_stats(c), tab="activity", jobs=js)


@app.get("/jobs/{jid}", response_class=HTMLResponse)
def job_detail(request: Request, jid: int):
    with SessionLocal() as db:
        j = get_or_404(db, Job, jid)
        return render(request, "job.html", c=j.client, s=client_stats(j.client), tab="activity", j=j)


@app.get("/clients/{cid}/report", response_class=HTMLResponse)
def client_report(request: Request, cid: int):
    """Client-facing summary: progress, rankings, what was done, what's next."""
    with SessionLocal() as db:
        c = get_client(db, cid)
        since = now() - dt.timedelta(days=30)
        done = sorted([r for r in c.recommendations if r.status in ("done", "resolved") and r.done_at and r.done_at > since],
                      key=lambda r: -r.score)
        tracked = sorted([k for k in c.keywords if k.tracked], key=lambda k: (-k.priority, k.position or 999))
        return render(request, "report.html", c=c, s=client_stats(c), done=done, next_up=open_recs(c)[:6],
                      tracked=tracked, today=dt.date.today())


@app.get("/settings", response_class=HTMLResponse)
def settings(request: Request):
    login = config.DATAFORSEO_LOGIN.strip()
    masked = (login[:3] + "…" + login[login.find("@"):]) if "@" in login else (login[:3] + "…" if login else "")
    return render(request, "settings.html", gsc_email=sources.gsc_service_email(), cfg=config, dfs_login=masked,
                  redirect_uri=_google_redirect_uri(request),
                  dfs_pw_len=len(config.DATAFORSEO_PASSWORD.strip()),
                  dfs_msg=request.query_params.get("dfs_msg", ""), dfs_ok=request.query_params.get("dfs_ok") == "1")


@app.post("/settings/test-dataforseo")
def settings_test_dfs():
    ok, msg = sources.dfs_test()
    return RedirectResponse(f"/settings?dfs_ok={int(ok)}&dfs_msg=" + quote(msg) + "#dataforseo", status_code=303)
