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
    mp.setattr(ai, "gbp_posts", lambda profile, kws, recent, n=8, month_hint="", location=None: [
        {"post_type": "update", "title": "Spring Botox refresh", "body": "Text", "cta": "BOOK", "link": BASE,
         "keyword": "botox newport beach", "image_idea": "Treatment room", "days_from_now": 2}])
    mp.setattr(ai, "blog_plan", lambda profile, kws, existing, questions, n=6: [
        {"title": "How Long Does Lip Filler Last?", "target_keyword": "how long does lip filler last",
         "secondary_keywords": [], "search_intent": "informational", "angle": "Top question", "outline": "## A",
         "supports_url": BASE + "services/lip-filler/", "priority": 5},
        {"title": "Botox vs Dysport", "target_keyword": "botox vs dysport", "secondary_keywords": [],
         "search_intent": "comparison", "angle": "Comparison", "outline": "## B", "supports_url": "", "priority": 4}])
    mp.setattr(ai, "write_blog", lambda profile, post, pages, comps: {
        "title": "How Long Does Lip Filler Last? A Newport Beach Guide", "meta_title": "How Long Does Lip Filler Last? | Glow MedSpa",
        "meta_description": "Lip filler usually lasts 6-12 months. Learn what affects longevity, how to make results last, and when to book a touch-up in Newport Beach.",
        "slug": "how-long-does-lip-filler-last", "excerpt": "x",
        "answer_summary": "How long does lip filler last? Most lip filler lasts 6 to 12 months depending on the product and your metabolism.",
        "key_takeaways": ["Lasts 6-12 months"],
        "body_markdown": "## How long does lip filler last?\n\nText with [our lip filler](" + BASE + "services/lip-filler/) and [about](" + BASE + "about/).\n\n[[visual:1]]\n\n## What affects it?\n\n[[visual:2]]\n\n## Aftercare\n\ntext <script>alert(1)</script>\n\n## Book\n\n[[visual:3]]",
        "faqs": [{"question": f"Q{i}?", "answer": "A."} for i in range(4)],
        "visuals": [{"kind": "table", "title": "Products", "caption": "c", "columns": ["Product", "Lasts"], "rows": [["Juvederm", "12 mo"]],
                     "labels": [], "values": [], "unit": "", "photo_brief": "", "alt_text": "table"},
                    {"kind": "bar_chart", "title": "Months", "caption": "c", "columns": [], "rows": [], "labels": ["A", "B"],
                     "values": [6, 12], "unit": "months", "photo_brief": "", "alt_text": "chart"},
                    {"kind": "photo", "title": "", "caption": "c", "columns": [], "rows": [], "labels": [], "values": [], "unit": "",
                     "photo_brief": "Treatment room", "alt_text": "treatment room"}],
        "references": ["FDA"]})
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
        assert {"on_page", "content", "new_page"} <= cats
        assert len([b for b in c.blog_posts if b.status == "idea"]) == 2
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


class FakeWordPress(http.server.BaseHTTPRequestHandler):
    posts: dict = {}

    def log_message(self, *a):
        pass

    def _send(self, code, body):
        import json as _json
        data = _json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authed(self):
        import base64 as _b64
        return self.headers.get("Authorization") == "Basic " + _b64.b64encode(b"editor:app pass").decode()

    def do_GET(self):
        if not self._authed():
            return self._send(401, {"message": "bad login"})
        if self.path.startswith("/wp-json/wp/v2/users/me"):
            return self._send(200, {"name": "Editor", "roles": ["editor"]})
        self._send(404, {"message": "nope"})

    def do_POST(self):
        import json as _json
        if not self._authed():
            return self._send(401, {"message": "bad login"})
        body = _json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if "meta" in body:  # like a site without an SEO plugin exposing these fields
            return self._send(400, {"message": "Invalid parameter(s): meta"})
        pid = self.path.rstrip("/").rsplit("/", 1)[-1]
        pid = pid if pid.isdigit() else str(100 + len(self.posts))
        self.posts[pid] = body
        self._send(200, {"id": int(pid)})


def test_blog_engine_and_wordpress_sync():
    from fastapi.testclient import TestClient
    from seoapp.db import BlogPost
    from seoapp.main import app

    wp_server = http.server.ThreadingHTTPServer(("127.0.0.1", 8802), FakeWordPress)
    threading.Thread(target=wp_server.serve_forever, daemon=True).start()
    try:
        with SessionLocal() as db:
            c = db.scalars(__import__("sqlalchemy").select(Client)).first()
            cid = c.id
            idea = next(b for b in c.blog_posts if b.target_keyword == "how long does lip filler last")
            bid = idea.id
        run("blog_write", cid, post_id=bid)
        with TestClient(app) as tc:
            page = tc.get(f"/blog/{bid}").text
            assert "Quick answer" in page and "<table>" in page and "months" in page and "Add photo" in page
            assert "<script>alert" not in page  # model output is escaped in the preview
            assert "of 13 passed" in page
            r = tc.post(f"/clients/{cid}/wordpress", data={"wp_url": "http://127.0.0.1:8802", "wp_user": "editor",
                                                          "wp_app_password": "wrong", "action": "test"})
            assert "Connection failed" in r.text
            r = tc.post(f"/clients/{cid}/wordpress", data={"wp_url": "http://127.0.0.1:8802", "wp_user": "editor",
                                                          "wp_app_password": "app pass", "action": "test"})
            assert "Connected as Editor" in r.text
            tc.post(f"/blog/{bid}/status", data={"status": "approved"})
            r = tc.post(f"/blog/{bid}/sync")
            assert "Sent to WordPress as a draft" in r.text
            assert "Open the draft in WordPress" in r.text
            sent = FakeWordPress.posts["100"]
            assert sent["status"] == "draft" and sent["slug"] == "how-long-does-lip-filler-last"
            assert "application/ld+json" in sent["content"] and "FAQPage" in sent["content"]
            tc.post(f"/blog/{bid}/sync")  # second sync updates the same draft
            assert list(FakeWordPress.posts) == ["100"]
            r = tc.post(f"/clients/{cid}/blog/new", data={"title": "Our spring specials", "notes": "x"},
                        follow_redirects=False)
            assert r.status_code == 303
            assert tc.get(f"/clients/{cid}/blog").status_code == 200
            assert tc.get(f"/blog/{bid}/export.html").status_code == 200
        with SessionLocal() as db:
            assert db.get(BlogPost, bid).status == "synced"
    finally:
        wp_server.shutdown()


def test_multiple_locations():
    from fastapi.testclient import TestClient
    from seoapp import locations
    from seoapp.db import GbpPost, Keyword
    from seoapp.main import app

    with SessionLocal() as db:
        c = Client(name="Akari", url="https://akari.example/", gbp={"primary_category": "Medical spa", "_audited": "2026-01-01"},
                   profile={"business_name": "Akari MedSpa", "locations": [
                       {"name": "Newport Beach", "city": "Newport Beach", "address": "1 Bristol St", "phone": "1",
                        "dataforseo_location": "Newport Beach,California,United States", "service_areas": ["Irvine", "Costa Mesa"]},
                       {"name": "Pasadena", "city": "Pasadena", "address": "202 S Lake Ave", "phone": "2",
                        "dataforseo_location": "Pasadena,California,United States", "service_areas": ["Arcadia", "San Marino"]}]})
        db.add(c)
        db.commit()
        cid = c.id
        locs = locations.all_for(c)
        assert [l["key"] for l in locs] == ["newport-beach", "pasadena"]
        # older single-profile GBP data belongs to the primary location
        assert locations.gbp_for(c, "newport-beach")["primary_category"] == "Medical spa"
        assert locations.gbp_for(c, "pasadena") == {}
        # keywords are searched from the city they name; generic ones from every location
        kw = lambda text, loc="": Keyword(keyword=text, location=loc)
        place, ls = locations.rank_location_for(c, kw("botox arcadia"))
        assert place == "Arcadia,California,United States" and [l["key"] for l in ls] == ["pasadena"]
        place, ls = locations.rank_location_for(c, kw("lip filler pasadena", "Pasadena"))
        assert place == "Pasadena,California,United States"
        _, ls = locations.rank_location_for(c, kw("botox near me"))
        assert len(ls) == 2

    run("gbp_posts", cid, location="pasadena")
    with SessionLocal() as db:
        assert {p.location for p in db.get(Client, cid).posts} == {"pasadena"}

    with TestClient(app) as tc:
        page = tc.get(f"/clients/{cid}/gbp?loc=pasadena").text
        assert "202 S Lake Ave" in page and "Spring Botox refresh" in page
        assert "Spring Botox refresh" not in tc.get(f"/clients/{cid}/gbp?loc=newport-beach").text
        tc.post(f"/clients/{cid}/gbp?loc=pasadena", data={"primary_category": "Skin care clinic"})
        with SessionLocal() as db:
            c = db.get(Client, cid)
            assert locations.gbp_for(c, "pasadena")["primary_category"] == "Skin care clinic"
            assert locations.gbp_for(c, "newport-beach")["primary_category"] == "Medical spa"
        assert tc.get(f"/clients/{cid}/blog").status_code == 200
        assert tc.get(f"/clients/{cid}/settings").status_code == 200
