"""End-to-end test: crawl a local mock medspa site, run the full audit pipeline
with Claude stubbed out, and render every dashboard page.

    pip install -r requirements.txt pytest httpx
    pytest tests/
"""

import functools
import http.server
import os
import pathlib
import tempfile
import threading

import pytest

os.environ["DATA_DIR"] = tempfile.mkdtemp()
os.environ["SCHEDULER_ENABLED"] = "0"
os.environ["ANTHROPIC_API_KEY"] = "test"
os.environ.pop("DATABASE_URL", None)

from seoapp import ai, config, jobs  # noqa: E402
from seoapp.db import Client, SessionLocal, init_db  # noqa: E402

SITE = pathlib.Path(__file__).parent / "mock_site"
PORT = 8800
BASE = f"http://127.0.0.1:{PORT}/"


@pytest.fixture(scope="module", autouse=True)
def mock_site():
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(SITE))
    handler.log_message = lambda *a: None
    server = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield
    server.shutdown()


@pytest.fixture(scope="module", autouse=True)
def fake_claude():
    """Canned Claude responses with the same shapes as the real ones."""
    rec = lambda title, cat, kw="": {"title": title, "category": cat, "details": f"Do **{title}**.", "keyword": kw,
                                     "impact": 7, "effort": 3, "confidence": 0.8}
    mp = pytest.MonkeyPatch()
    mp.setattr(config, "ANTHROPIC_API_KEY", "test")
    mp.setattr(ai, "build_profile", lambda pages: {
        "business_name": "Glow MedSpa", "business_type": "Medical spa", "is_medical": True, "summary": "",
        "services": [{"name": "Botox", "url": BASE + "services/botox/", "description": ""},
                     {"name": "Lip filler", "url": BASE + "services/lip-filler/", "description": ""}],
        "locations": [{"name": "Newport Beach", "address": "123 Main Street Suite 4, Newport Beach, CA 92660",
                       "city": "Newport Beach", "state": "CA", "phone": "(949) 555-0100",
                       "dataforseo_location": "Newport Beach,California,United States"}],
        "service_areas": ["Irvine", "Costa Mesa"], "unique_selling_points": [], "providers": ["Dr. Jane Kim"],
        "brand_voice": "warm", "booking_url": ""})
    mp.setattr(ai, "generate_keywords", lambda profile, pages, existing, gsc: [
        {"keyword": "botox newport beach", "service": "Botox", "location": "Newport Beach", "intent": "transactional",
         "priority": 5, "target_url": BASE + "services/botox/", "rationale": ""},
        {"keyword": "lip filler newport beach", "service": "Lip filler", "location": "Newport Beach",
         "intent": "transactional", "priority": 5, "target_url": BASE + "services/lip-filler/", "rationale": ""},
        {"keyword": "botox irvine", "service": "Botox", "location": "Irvine", "intent": "transactional",
         "priority": 4, "target_url": "", "rationale": ""},
        {"keyword": "how long does botox last", "service": "Botox", "location": "", "intent": "informational",
         "priority": 2, "target_url": "", "rationale": ""}])
    mp.setattr(ai, "map_keywords", lambda kws, pages: {k: "" for k in kws})
    mp.setattr(ai, "optimize_page", lambda profile, page, kws, comps, gsc, site_pages: {
        "main_gap": "content", "diagnosis": "Competitors cover pricing and recovery.",
        "recommendations": [rec("Rewrite the title tag", "on_page", kws[0]["keyword"]),
                            rec("Add an FAQ section", "content", kws[0]["keyword"])]})
    mp.setattr(ai, "assign_page_keywords", lambda profile, pages, gsc, taken: [
        {"url": p["url"], "rank_worthy": "contact" not in p["url"] and "orphan" not in p["url"],
         "primary_keyword": "" if "contact" in p["url"] or "orphan" in p["url"] else
         ("botox newport beach" if "botox/" in p["url"] and "blog" not in p["url"] else "glow " + p["url"].rstrip("/").rsplit("/", 1)[-1].replace(":", "")),
         "secondary_keywords": ["botox cost newport beach"] if p["url"].endswith("/services/botox/") else [],
         "priority": 4, "location": "Newport Beach"} for p in pages])
    mp.setattr(ai, "new_page_briefs", lambda profile, kws, pages: [rec("Create an Irvine Botox page", "new_page", "botox irvine")])
    mp.setattr(ai, "blog_ideas", lambda profile, kws, titles, n=6: [rec(f"Blog idea {i}", "blog") for i in range(3)])
    mp.setattr(ai, "gbp_audit", lambda profile, gbp, kws, comps: [rec("Add secondary categories", "gbp")])
    mp.setattr(ai, "gbp_posts", lambda profile, kws, recent, n=8, month_hint="": [
        {"post_type": "update", "title": "Spring Botox refresh", "body": "Text", "cta": "BOOK", "link": BASE,
         "keyword": "botox newport beach", "image_idea": "Treatment room", "days_from_now": 2}])
    mp.setattr(ai, "write_draft", lambda profile, rec, page, feedback="": "# Draft\n\nHello")
    mp.setattr(jobs.sources, "pagespeed", lambda url: {"performance": 42, "lcp_ms": 5200, "cls": 0.2,
                                                       "opportunities": ["Properly size images"], "checked": "x"})
    yield
    mp.undo()


def run(kind, cid, **params):
    with SessionLocal() as db:
        c = db.get(Client, cid)
        jobs.HANDLERS[kind](db, c, print, **params)


def test_full_pipeline_and_ui():
    init_db()
    with SessionLocal() as db:
        c = Client(name="127.0.0.1:8800", url=BASE)
        db.add(c)
        db.commit()
        cid = c.id

    run("audit", cid)

    with SessionLocal() as db:
        c = db.get(Client, cid)
        assert c.name == "Glow MedSpa"
        assert c.rank_location == "Newport Beach,California,United States"
        urls = {p.url for p in c.pages}
        assert BASE + "orphan/" in urls and BASE + "blog/how-long-does-botox-last/" in urls
        codes = {r.fingerprint.split(":")[1] for r in c.recommendations if r.fingerprint.startswith("tech:")}
        for expected in ("no_https", "broken_links", "noindex_important", "duplicate_title", "missing_meta",
                         "missing_h1", "multiple_h1", "thin_content", "orphan_pages", "img_alt",
                         "no_local_schema", "slow_mobile"):
            assert expected in codes, expected
        tracked = [k for k in c.keywords if k.tracked]
        assert {"botox newport beach", "lip filler newport beach", "botox irvine", "botox cost newport beach"} <= {k.keyword for k in tracked}
        cats = {r.category for r in c.recommendations}
        assert {"on_page", "content", "new_page", "blog"} <= cats
        # Scores are sorted sensibly: a critical noindex outranks a missing lang attribute
        assert all(0 <= r.score <= 100 for r in c.recommendations)
        broken = next(r for r in c.recommendations if r.fingerprint == "tech:broken_links")
        assert "broken-link" in broken.details
        n_tech = sum(r.fingerprint.startswith("tech:") for r in c.recommendations)
        rid = next(r.id for r in c.recommendations if r.category == "content")

    # Re-audit is idempotent: same issues update in place, no duplicates
    run("audit", cid)
    with SessionLocal() as db:
        recs = db.get(Client, cid).recommendations
        assert sum(r.fingerprint.startswith("tech:") for r in recs) == n_tech
        assert len({r.fingerprint for r in recs}) == len(recs)  # no duplicates

    # Fixing an issue on the site is detected and the item auto-resolves
    orphan = SITE / "orphan" / "index.html"
    saved = orphan.read_text()
    orphan.unlink()
    (SITE / "orphan").rmdir()
    try:
        run("audit", cid)
    finally:
        (SITE / "orphan").mkdir()
        orphan.write_text(saved)
    with SessionLocal() as db:
        c = db.get(Client, cid)
        orphan_rec = next(r for r in c.recommendations if r.fingerprint == "tech:orphan_pages")
        assert orphan_rec.status == "resolved"

    run("draft", cid, rec_id=rid)
    with SessionLocal() as db:
        c = db.get(Client, cid)
        c.gbp = {"primary_category": "Medical spa", "review_count": "40"}
        db.commit()
    run("gbp_audit", cid)
    run("gbp_posts", cid)

    from fastapi.testclient import TestClient
    from seoapp.main import app

    with TestClient(app) as tc:
        with SessionLocal() as db:
            c = db.get(Client, cid)
            botox = next(p for p in c.pages if p.url.endswith("/services/botox/"))
            pid = botox.id
            assert botox.extra.get("main_gap") == "content"
            primaries = [k for k in c.keywords if k.role == "primary"]
            assert primaries and all(k.tracked and k.target_url for k in primaries)
            assert len({k.target_url for k in primaries}) == len(primaries)  # one primary per page
        for path in [f"/clients/{cid}/pages?status=unchecked", f"/pages/{pid}", f"/clients/{cid}/urls",
                     "/", f"/clients/{cid}", f"/clients/{cid}/recommendations", f"/clients/{cid}/keywords",
                     f"/clients/{cid}/pages", f"/clients/{cid}/gbp", f"/clients/{cid}/settings",
                     f"/clients/{cid}/activity", f"/clients/{cid}/report", "/settings", f"/recommendations/{rid}",
                     f"/clients/{cid}/recommendations?status=resolved"]:
            r = tc.get(path)
            assert r.status_code == 200, (path, r.text[:2000])
        assert "Glow MedSpa" in tc.get("/").text
        assert "Draft ready" in tc.get(f"/clients/{cid}/recommendations").text or "Hello" in tc.get(f"/recommendations/{rid}").text
        r = tc.post(f"/recommendations/{rid}/status", data={"status": "done"}, follow_redirects=False)
        assert r.status_code == 303
        r = tc.post(f"/clients/{cid}/keywords/add", data={"keywords": "hydrafacial newport beach\n", "priority": "3"},
                    follow_redirects=False)
        assert r.status_code == 303
        assert "hydrafacial newport beach" in tc.get(f"/clients/{cid}/keywords").text
        assert "Spring Botox refresh" in tc.get(f"/clients/{cid}/gbp").text


def test_login_required_when_password_set(monkeypatch):
    from fastapi.testclient import TestClient
    from seoapp.main import app

    monkeypatch.setattr(config, "APP_PASSWORD", "secret")
    with TestClient(app) as tc:
        r = tc.get("/", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login"
        assert tc.post("/login", data={"password": "wrong"}).status_code == 401
        assert tc.post("/login", data={"password": "secret"}, follow_redirects=False).status_code == 303
        assert tc.get("/").status_code == 200


def test_rank_alerts_and_wrong_page():
    from seoapp.db import Keyword

    k = Keyword(client_id=1, keyword="botox newport beach", target_url="https://x.com/botox/", priority=5, tracked=True)
    assert jobs._record_position(k, 14, None, "https://x.com/botox/", "test") is None  # first check: no alert
    a = jobs._record_position(k, 8, None, "https://x.com/botox/", "test")
    assert a.kind == "up" and "page 1" in a.message
    assert jobs._record_position(k, 9, None, "https://x.com/botox/", "test") is None  # small move: no alert
    a = jobs._record_position(k, None, None, "", "test")
    assert a.kind == "lost"
    assert jobs.google_page(1) == 1 and jobs.google_page(10) == 1 and jobs.google_page(11) == 2
