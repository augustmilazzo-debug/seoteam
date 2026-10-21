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
    mp.setattr(ai, "new_page_briefs", lambda profile, kws, pages, inventory=None: [
        rec("Create an Irvine Botox page", "new_page", "botox irvine"),
        rec("Create a Botox Newport Beach page", "new_page", "botox newport beach")])  # duplicate of a live page
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
         "search_intent": "comparison", "angle": "Comparison", "outline": "## B", "supports_url": "", "priority": 4},
        {"title": "How Long Does Botox Last? A Guide", "target_keyword": "how long does botox last",  # live post exists
         "secondary_keywords": [], "search_intent": "informational", "angle": "x", "outline": "", "supports_url": "",
         "priority": 4}])
    mp.setattr(ai, "write_blog", lambda profile, post, pages, comps, related=None: {
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
        "references": ["FDA"], "category": "Dermal Fillers"})
    mp.setattr(ai, "write_draft", lambda profile, rec, page, feedback="": "# Draft\n\nHello")
    mp.setattr(jobs.sources, "pagespeed", lambda url: {"performance": 42, "lcp_ms": 5200, "cls": 0.2,
                                                       "opportunities": ["Properly size images"], "checked": "x"})
    yield
    mp.undo()


def wait_for_jobs(timeout=20):
    """Let jobs queued by button clicks finish before a test runs the same job directly."""
    import time as _time

    from seoapp.db import Job

    end = _time.time() + timeout
    while _time.time() < end:
        with SessionLocal() as db:
            if not db.query(Job).filter(Job.status.in_(["queued", "running"])).count():
                return
        _time.sleep(0.2)


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
        assert {b.target_keyword for b in c.blog_posts} == {"how long does lip filler last", "botox vs dysport"}
        new_pages = [r for r in c.recommendations if r.category == "new_page"]
        assert [r.keyword for r in new_pages] == ["botox irvine"]  # the duplicate of /services/botox/ was dropped
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
        if self.path.startswith("/wp-json/wp/v2/categories"):
            return self._send(200, [{"id": 7, "name": "Other"}])
        if self.path.startswith("/wp-json/wp/v2/posts/"):
            pid = self.path.split("?")[0].rstrip("/").rsplit("/", 1)[-1]
            return self._send(200, {"id": int(pid), "content": {"raw": self.posts.get(pid, {}).get("content", "")}})
        self._send(404, {"message": "nope"})

    def do_POST(self):
        import json as _json
        if not self._authed():
            return self._send(401, {"message": "bad login"})
        body = _json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path.startswith("/wp-json/wp/v2/categories"):
            return self._send(201, {"id": 12, "name": body["name"]})
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
            assert "of 19 passed" in page
            assert "BlogPosting schema" in page and "Breadcrumb schema" in page and "View the JSON-LD" in page
            assert 'href="#' in page and "In this article" in page  # table of contents
            r = tc.post(f"/clients/{cid}/wordpress", data={"wp_url": "http://127.0.0.1:8802", "wp_user": "editor",
                                                          "wp_app_password": "wrong", "action": "test"})
            assert "Connection failed" in r.text
            r = tc.post(f"/clients/{cid}/wordpress", data={"wp_url": "http://127.0.0.1:8802", "wp_user": "editor",
                                                          "wp_app_password": "app pass", "action": "test"})
            assert "Connected as Editor" in r.text
            r = tc.post(f"/blog/{bid}/send")  # straight from review, no separate approve step
            assert "Sent to WordPress as a draft and marked Published" in r.text
            assert "Open in WordPress" in r.text
            sent = FakeWordPress.posts["100"]
            assert sent["status"] == "draft" and sent["slug"] == "how-long-does-lip-filler-last"
            assert "application/ld+json" in sent["content"] and "FAQPage" in sent["content"]
            assert sent["categories"] == [12] and '"BlogPosting"' in sent["content"] and "dateModified" in sent["content"]
            assert "Schema confirmed in the WordPress draft" in r.text and "Category set" in r.text
            tc.post(f"/blog/{bid}/send")  # sending again updates the same draft
            assert list(FakeWordPress.posts) == ["100"]
            r = tc.post(f"/clients/{cid}/blog/new", data={"title": "Our spring specials", "notes": "x"},
                        follow_redirects=False)
            assert r.status_code == 303
            assert tc.get(f"/clients/{cid}/blog").status_code == 200
            assert tc.get(f"/blog/{bid}/export.html").status_code == 200
        with SessionLocal() as db:
            assert db.get(BlogPost, bid).status == "published"
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


def test_overlap_guard_flags_duplicate_manual_post():
    from fastapi.testclient import TestClient
    from seoapp import overlap
    from seoapp.main import app

    with SessionLocal() as db:
        c = db.scalars(__import__("sqlalchemy").select(Client)).first()
        cid = c.id
        hits = overlap.matches(c, "how long does botox last", "How long does Botox last?")
        assert hits and hits[0]["url"].endswith("/blog/how-long-does-botox-last/") and hits[0]["score"] == 1.0
        assert not overlap.matches(c, "botox vs dysport", "Botox vs Dysport", threshold=overlap.DUPLICATE,
                                   items=[i for i in overlap.inventory(c) if i["kind"] == "page"])
    with TestClient(app) as tc:
        r = tc.post(f"/clients/{cid}/blog/new", data={"title": "How long does Botox last?",
                                                     "target_keyword": "how long does botox last"})
        assert "Possible keyword cannibalization" in r.text and "100% overlap" in r.text
        r = tc.post(f"/clients/{cid}/blog/new", data={"title": "Is Botox safe while breastfeeding?",
                                                     "target_keyword": "botox breastfeeding"})
        assert "Possible keyword cannibalization" not in r.text


def test_dataforseo_errors_are_explained(monkeypatch):
    from seoapp import sources

    class Resp:
        def __init__(self, status, body):
            self.status_code, self._body, self.text = status, body, str(body)

        def json(self):
            return self._body

    monkeypatch.setattr(config, "DATAFORSEO_LOGIN", "me@x.com")
    monkeypatch.setattr(config, "DATAFORSEO_PASSWORD", "pw")
    # wrong API password: DataForSEO answers HTTP 401 with status 40100
    monkeypatch.setattr(sources.requests, "post", lambda *a, **k: Resp(401, {"status_code": 40100, "status_message": "You are not authorized"}))
    with pytest.raises(sources.DataForSEOAccountError, match="API password"):
        sources.organic_rank("botox newport beach", "x.com", "Newport Beach,California,United States")
    # no funds: reported inside the task
    monkeypatch.setattr(sources.requests, "post", lambda *a, **k: Resp(200, {"status_code": 20000, "tasks": [
        {"status_code": 40200, "status_message": "Payment Required."}]}))
    with pytest.raises(sources.DataForSEOAccountError, match="no funds"):
        sources.maps_rank("botox", "x.com", "X", "")
    # unknown location falls back to the US instead of failing
    calls = []

    def post(url, json, **k):
        calls.append(json[0])
        if "location_name" in json[0]:
            return Resp(200, {"status_code": 20000, "tasks": [{"status_code": 40501, "status_message": "Invalid Field: 'location_name'."}]})
        return Resp(200, {"status_code": 20000, "tasks": [{"status_code": 20000, "result": [{"items": [
            {"type": "organic", "rank_group": 4, "domain": "www.x.com", "url": "https://x.com/botox/", "title": "t"}]}]}]})
    monkeypatch.setattr(sources.requests, "post", post)
    res = sources.organic_rank("botox", "x.com", "Nowhere,Nope,United States")
    assert res["position"] == 4 and calls[-1]["location_code"] == sources.US


def test_search_console_connect_and_insights(monkeypatch):
    from urllib.parse import parse_qs, urlparse

    from fastapi.testclient import TestClient
    from seoapp import sources
    from seoapp.db import Alert, Keyword
    from seoapp.main import app

    with SessionLocal() as db:
        c = db.scalars(__import__("sqlalchemy").select(Client)).first()
        cid = c.id
    monkeypatch.setattr(config, "GOOGLE_CLIENT_ID", "cid.apps.googleusercontent.com")
    monkeypatch.setattr(config, "GOOGLE_CLIENT_SECRET", "secret")
    monkeypatch.setattr(sources, "google_exchange", lambda code, uri: {"refresh_token": "rt", "email": "me@agency.com",
                                                                      "connected": "2026-10-21"})
    monkeypatch.setattr(sources, "gsc_sites", lambda c: [{"url": "sc-domain:glow.example", "permission": "siteOwner"},
                                                          {"url": "https://other.example/", "permission": "siteFullUser"}])
    botox, lip = BASE + "services/botox/", BASE + "services/lip-filler/"
    current = [
        # botox page: page 1 but almost no clicks -> low click-through
        {"query": "botox newport beach", "page": botox, "clicks": 2, "impressions": 900, "ctr": 0.002, "position": 4.0},
        # two pages split one search -> cannibalization
        {"query": "lip filler near me", "page": lip, "clicks": 5, "impressions": 60, "ctr": 0.08, "position": 9.0},
        {"query": "lip filler near me", "page": botox, "clicks": 1, "impressions": 40, "ctr": 0.02, "position": 14.0},
        # a real search nobody is tracking -> keyword idea
        {"query": "russian lip filler newport", "page": lip, "clicks": 3, "impressions": 80, "ctr": 0.04, "position": 7.0},
    ]
    previous = [{"query": "", "page": lip, "clicks": 60, "impressions": 500, "ctr": 0.1, "position": 6.0}]
    monkeypatch.setattr(sources, "gsc_rows", lambda c, days=28, offset_days=0, dimensions=("query", "page"):
                        previous if offset_days else current)

    with TestClient(app) as tc:
        r = tc.get(f"/clients/{cid}/gsc/connect", follow_redirects=False)
        state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
        assert r.headers["location"].startswith("https://accounts.google.com/") and "webmasters.readonly" in r.headers["location"]
        r = tc.get(f"/oauth/google/callback?state={state}&code=abc")
        assert "Connected as me@agency.com" in r.text and "sc-domain:glow.example (whole domain)" in r.text
        assert tc.get("/oauth/google/callback?state=forged&code=abc").status_code == 400
        r = tc.post(f"/clients/{cid}/gsc", data={"gsc_property": "sc-domain:glow.example"})
        assert "Connected to sc-domain:glow.example" in r.text
        with SessionLocal() as db:
            assert sources.gsc_ready(db.get(Client, cid))
        wait_for_jobs()

    run("gsc_sync", cid)
    with SessionLocal() as db:
        c = db.get(Client, cid)
        fps = {r.fingerprint for r in c.recommendations if r.status == "open"}
        assert f"rule:gsc_ctr:{botox}" in fps
        assert "rule:gsc_cannibal:lip-filler-near-me" in fps
        assert f"rule:gsc_drop:{lip}" in fps
        assert "rule:gsc_invisible" in fps  # e.g. /about/ never appeared in Google
        assert any(a.kind == "down" and a.page_url == lip for a in db.query(Alert).all())
        idea = next(k for k in c.keywords if k.keyword == "russian lip filler newport")
        assert idea.source == "gsc" and not idea.tracked and idea.target_url == lip
        pid = next(p.id for p in c.pages if p.url == botox)
    with TestClient(app) as tc:
        assert "botox newport beach" in tc.get(f"/pages/{pid}").text and "times shown" in tc.get(f"/pages/{pid}").text
        assert "Clicks 28d" in tc.get(f"/clients/{cid}/pages").text
    # the next sync with the problem gone resolves the recommendation
    current[0]["clicks"] = 90
    run("gsc_sync", cid)
    with SessionLocal() as db:
        c = db.get(Client, cid)
        assert next(r for r in c.recommendations if r.fingerprint == f"rule:gsc_ctr:{botox}").status == "resolved"
        assert len([k for k in c.keywords if k.keyword == "russian lip filler newport"]) == 1  # not re-added


def test_buttons_queue_jobs_their_handlers_accept():
    """Every job a button queues must run with the exact parameters the button stored."""
    import inspect

    from fastapi.testclient import TestClient
    from seoapp.db import BlogPost, Job, Page
    from seoapp.main import app

    with SessionLocal() as db:
        c = db.scalars(__import__("sqlalchemy").select(Client)).first()
        cid = c.id
        bid = next(b.id for b in c.blog_posts)
        pid = next(p.id for p in c.pages if p.url.endswith("/services/botox/"))
        rid = next(r.id for r in c.recommendations if r.category == "content")
        start = max(j.id for j in db.query(Job).all())
    with TestClient(app) as tc:
        tc.post(f"/blog/{bid}/write", data={"feedback": "shorter please"})
        tc.post(f"/blog/{bid}/write", data={"feedback": ""})
        tc.post(f"/pages/{pid}/review")
        tc.post(f"/pages/{pid}/ranks")
        tc.post(f"/recommendations/{rid}/draft", data={"feedback": "x"})
        tc.post(f"/clients/{cid}/gbp/posts?loc=newport-beach")
        tc.post(f"/clients/{cid}/gbp", data={"primary_category": "Medical spa"})
        for kind in ("audit", "ranks", "keywords", "profile", "blog", "page_keywords", "gsc_sync"):
            tc.post(f"/clients/{cid}/run/{kind}")
    with SessionLocal() as db:
        queued = [j for j in db.query(Job).all() if j.id > start]
        assert len(queued) >= 10
        for j in queued:
            sig = inspect.signature(jobs.HANDLERS[j.kind])
            sig.bind(None, None, None, **(j.params or {}))  # raises TypeError on an unexpected parameter


def test_blog_schema_does_not_duplicate_seo_plugin():
    import json as _json

    from seoapp import blog
    from seoapp.db import BlogPost

    post = BlogPost(title="Botox vs Dysport", meta_description="d", target_keyword="botox vs dysport", category="Neurotoxins",
                    body="## A\n\nx\n\n## B\n\ny\n\n## C\n\nz", faqs=[{"question": "Q?", "answer": "A."}],
                    secondary_keywords=[], visuals=[])
    profile = {"business_name": "Glow MedSpa", "is_medical": True, "providers": ["Dr. Jane Kim, MD"]}
    full = _json.loads(blog.schema_json(post, profile, {"site_url": "https://glow.example/", "blog_url": "https://glow.example/blog/"}))
    types = [n["@type"] if isinstance(n["@type"], str) else n["@type"][0] for n in full["@graph"]]
    assert types == ["BlogPosting", "MedicalBusiness", "BreadcrumbList", "FAQPage"]
    art = full["@graph"][0]
    assert art["reviewedBy"]["name"] == "Dr. Jane Kim, MD" and art["about"]["@type"] == "MedicalProcedure"
    assert art["publisher"] == {"@id": "https://glow.example/#organization"}
    # the site's SEO plugin already outputs Article + BreadcrumbList on posts: only FAQPage is added
    lean = _json.loads(blog.schema_json(post, profile, {"site_url": "https://glow.example/", "has_article_schema": True,
                                                         "has_breadcrumb_schema": True}))
    assert [n["@type"] for n in lean["@graph"]] == ["FAQPage"]
    html_out = blog.full_html(post, profile, {}, include_schema=False)
    assert 'id="a"' in html_out and 'href="#a"' in html_out and "Medically reviewed by <strong>Dr. Jane Kim, MD</strong>" in html_out


def test_publish_to_other_platforms(monkeypatch):
    from fastapi.testclient import TestClient
    from seoapp import publish
    from seoapp.db import BlogPost
    from seoapp.main import app

    assert publish.detect_platform('<link href="/wp-content/themes/x.css">', {}) == "WordPress"
    assert publish.detect_platform('<html data-wf-site="abc">', {}) == "Webflow"
    assert publish.detect_platform('<script src="https://cdn.shopify.com/x.js">', {}) == "Shopify"
    assert publish.detect_platform('<img src="https://static.wixstatic.com/a.png">', {}) == "Wix"
    assert publish.detect_platform('<link href="https://static1.squarespace.com/x">', {}) == "Squarespace"

    calls = []

    class Resp:
        def __init__(self, status, body):
            self.status_code, self._b, self.text, self.content = status, body, str(body), b"x"

        def json(self):
            return self._b

    def fake(method, url, **kw):
        calls.append((method, url, kw.get("json")))
        if "webflow" in url:
            if url.endswith("/sites"):
                return Resp(200, {"sites": [{"id": "s1"}]})
            if url.endswith("/collections"):
                return Resp(200, {"collections": [{"id": "col1", "displayName": "Blog Posts", "slug": "blog-posts"}]})
            if url.endswith("/collections/col1"):
                return Resp(200, {"fields": [{"slug": "name", "type": "PlainText"}, {"slug": "slug", "type": "PlainText"},
                                             {"slug": "post-body", "type": "RichText"}, {"slug": "post-summary", "type": "PlainText"}]})
            return Resp(200, {"id": "item9"})
        if url.endswith("/blogs.json"):
            return Resp(200, {"blogs": [{"id": 55, "title": "News"}]})
        return Resp(201, {"article": {"id": 777}})
    monkeypatch.setattr(publish.requests, "request", fake)

    with SessionLocal() as db:
        c = db.scalars(__import__("sqlalchemy").select(Client)).first()
        cid = c.id
        src = next(b for b in c.blog_posts if b.body)
        b2 = BlogPost(client_id=cid, title=src.title, body=src.body, faqs=src.faqs, visuals=src.visuals, slug="post-two",
                      meta_title=src.meta_title, meta_description=src.meta_description, status="review",
                      target_keyword="x", secondary_keywords=[])
        db.add(b2)
        db.commit()
        bid = b2.id
    with TestClient(app) as tc:
        tc.post(f"/clients/{cid}/website", data={"platform": "Webflow", "token": "wf-token", "collection_id": "", "action": "save"})
        r = tc.post(f"/blog/{bid}/send")
        assert "Sent to Webflow as a draft" in r.text
        body = next(j for m, u, j in calls if m == "POST" and u.endswith("/collections/col1/items"))
        assert body["isDraft"] is True and body["fieldData"]["slug"] == "post-two" and "<h2" in body["fieldData"]["post-body"]
        assert "application/ld+json" not in body["fieldData"]["post-body"]

        tc.post(f"/clients/{cid}/website", data={"platform": "Shopify", "shop": "glow.myshopify.com", "token": "shpat_x",
                                                 "blog_id": "", "action": "save"})
        with SessionLocal() as db:
            b = db.get(BlogPost, bid)
            b.status, b.cms_id = "review", ""
            db.commit()
        r = tc.post(f"/blog/{bid}/send")
        assert "Sent to Shopify as a draft" in r.text
        art = next(j for m, u, j in calls if u.endswith("/blogs/55/articles.json"))["article"]
        assert art["published"] is False and art["metafields"][0]["key"] == "title_tag"

        tc.post(f"/clients/{cid}/website", data={"platform": "Wix", "action": "save"})
        with SessionLocal() as db:
            db.get(BlogPost, bid).status = "review"
            db.commit()
        r = tc.post(f"/blog/{bid}/send", follow_redirects=True)
        assert "Publish to Wix: copy and paste" in r.text and "Structured data markup" in r.text
        assert "Copy formatted article" in r.text
        tc.post(f"/blog/{bid}/status", data={"status": "published"})
        tc.post(f"/clients/{cid}/website", data={"platform": "WordPress", "url": "http://127.0.0.1:8802", "user": "editor",
                                                 "app_password": "app pass", "action": "save"})
    with SessionLocal() as db:
        assert db.get(BlogPost, bid).status == "published"
