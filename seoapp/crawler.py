"""Site crawler: discovers every internal HTML page (links + sitemaps) and
extracts the on-page SEO data the audit needs."""

import concurrent.futures as cf
import hashlib
import json
import re
import time
import urllib.robotparser
from dataclasses import dataclass, field
from urllib.parse import urldefrag, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

from . import config

SKIP_EXT = re.compile(
    r"\.(jpg|jpeg|png|gif|webp|svg|ico|pdf|zip|mp4|mov|mp3|css|js|xml|json|txt|woff2?|ttf|eot|avif)$", re.I
)
SKIP_PATH = re.compile(r"/(wp-admin|wp-json|cart|checkout|my-account|feed)(/|$)|[?&](replytocom|add-to-cart)=", re.I)
TEXT_LIMIT = 12000


def normalize(url: str) -> str:
    url, _ = urldefrag(url.strip())
    p = urlparse(url)
    scheme = p.scheme.lower() or "https"
    host = p.netloc.lower()
    path = p.path or "/"
    path = re.sub(r"/{2,}", "/", path)
    return urlunparse((scheme, host, path, "", p.query, ""))


def bare_host(url: str) -> str:
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


@dataclass
class CrawlResult:
    pages: list[dict] = field(default_factory=list)
    site: dict = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


class Crawler:
    def __init__(self, start_url: str, max_pages: int | None = None, log=print):
        if not start_url.startswith(("http://", "https://")):
            start_url = "https://" + start_url
        self.start = normalize(start_url)
        self.host = bare_host(self.start)
        self.max_pages = max_pages or config.MAX_PAGES
        self.log = log
        self.session = requests.Session()
        self.session.headers["User-Agent"] = config.USER_AGENT
        self.robots = urllib.robotparser.RobotFileParser()

    # -- helpers -------------------------------------------------------------
    def internal(self, url: str) -> bool:
        p = urlparse(url)
        return p.scheme in ("http", "https") and bare_host(url) == self.host

    def allowed(self, url: str) -> bool:
        if SKIP_EXT.search(urlparse(url).path) or SKIP_PATH.search(url):
            return False
        try:
            return self.robots.can_fetch(config.USER_AGENT, url)
        except Exception:
            return True

    def get(self, url: str, allow_redirects=False, timeout=20):
        return self.session.get(url, allow_redirects=allow_redirects, timeout=timeout)

    # -- site-level checks ---------------------------------------------------
    def site_checks(self) -> dict:
        site: dict = {"start_url": self.start}
        try:
            r = self.get(self.start, allow_redirects=True)
            site["home_status"] = r.status_code
            site["final_home_url"] = normalize(r.url)
            self.start = normalize(r.url)
            self.host = bare_host(self.start)
        except requests.RequestException as e:
            site["home_status"] = 0
            site["home_error"] = str(e)
            return site
        site["https"] = self.start.startswith("https://")
        try:
            r = self.get(self.start.replace("https://", "http://", 1), allow_redirects=True, timeout=10)
            site["http_redirects_to_https"] = r.url.startswith("https://")
        except requests.RequestException:
            site["http_redirects_to_https"] = None
        root = f"{urlparse(self.start).scheme}://{urlparse(self.start).netloc}"
        site["robots_txt"] = False
        site["sitemaps"] = []
        try:
            r = self.get(root + "/robots.txt", timeout=10)
            if r.status_code == 200 and "html" not in r.headers.get("content-type", ""):
                site["robots_txt"] = True
                self.robots.parse(r.text.splitlines())
                site["sitemaps"] = re.findall(r"(?im)^\s*sitemap:\s*(\S+)", r.text)
                site["robots_blocks_all"] = bool(re.search(r"(?im)^\s*disallow:\s*/\s*$", r.text)) and \
                    not self.robots.can_fetch("Googlebot", self.start)
            else:
                self.robots.parse([])
        except requests.RequestException:
            self.robots.parse([])
        if not site["sitemaps"]:
            site["sitemaps"] = [root + "/sitemap.xml", root + "/sitemap_index.xml"]
        return site

    def sitemap_urls(self, sitemaps: list[str]) -> tuple[list[str], list[str]]:
        found, valid, seen = [], [], set()
        queue = list(sitemaps)
        while queue and len(found) < self.max_pages * 3 and len(seen) < 50:
            sm = queue.pop(0)
            if sm in seen:
                continue
            seen.add(sm)
            try:
                r = self.get(sm, allow_redirects=True, timeout=15)
            except requests.RequestException:
                continue
            if r.status_code != 200 or "<" not in r.text[:200]:
                continue
            locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", r.text)
            if not locs:
                continue
            valid.append(sm)
            if "<sitemapindex" in r.text:
                queue.extend(locs)
            else:
                found.extend(normalize(u.replace("&amp;", "&")) for u in locs)
        return found, valid

    # -- page fetch/parse ----------------------------------------------------
    def fetch(self, url: str, depth: int) -> dict:
        page = {"url": url, "depth": depth, "status_code": 0, "redirect_to": "", "links_out": []}
        t0 = time.time()
        try:
            r = self.get(url)
        except requests.RequestException as e:
            page["error"] = str(e)[:300]
            return page
        page["response_ms"] = int((time.time() - t0) * 1000)
        page["status_code"] = r.status_code
        if r.is_redirect or r.status_code in (301, 302, 303, 307, 308):
            page["redirect_to"] = normalize(urljoin(url, r.headers.get("location", "")))
            return page
        ctype = r.headers.get("content-type", "")
        if r.status_code != 200 or "html" not in ctype:
            page["non_html"] = "html" not in ctype
            return page
        page.update(parse_html(url, r.text, r.headers.get("x-robots-tag", "")))
        return page

    def crawl(self) -> CrawlResult:
        res = CrawlResult()
        res.site = self.site_checks()
        if not res.site.get("home_status"):
            res.errors.append(f"Could not reach {self.start}: {res.site.get('home_error')}")
            return res
        sm_urls, valid_sitemaps = self.sitemap_urls(res.site["sitemaps"])
        res.site["sitemaps"] = valid_sitemaps
        res.site["sitemap_url_count"] = len(sm_urls)
        sm_internal = [u for u in sm_urls if self.internal(u)]
        res.site["sitemap_urls"] = sm_internal[: self.max_pages * 2]
        self.log(f"Sitemaps: {len(valid_sitemaps)} found, {len(sm_internal)} URLs listed")

        depth_of: dict[str, int] = {self.start: 0}
        queue = [self.start] + [u for u in sm_internal if u != self.start]
        for u in sm_internal:
            depth_of.setdefault(u, 99)  # only reachable from sitemap until a link is found
        queued = set(queue)
        results: dict[str, dict] = {}

        with cf.ThreadPoolExecutor(max_workers=6) as pool:
            while queue and len(results) < self.max_pages:
                batch = []
                while queue and len(batch) < 12 and len(results) + len(batch) < self.max_pages:
                    u = queue.pop(0)
                    if u in results or not self.allowed(u):
                        continue
                    batch.append(u)
                for page in pool.map(lambda u: self.fetch(u, depth_of.get(u, 99)), batch):
                    results[page["url"]] = page
                    targets = list(page.get("links_out", []))
                    if page.get("redirect_to"):
                        targets.append(page["redirect_to"])
                    for link in targets:
                        if not self.internal(link):
                            continue
                        new_depth = page["depth"] + 1
                        if new_depth < depth_of.get(link, 999):
                            depth_of[link] = new_depth
                        if link not in queued:
                            queued.add(link)
                            queue.append(link)
                self.log(f"Crawled {len(results)} URLs ({len(queue)} queued)")

        # Inlinks and final depths
        inlinks: dict[str, int] = {}
        for p in results.values():
            for link in set(p.get("links_out", [])):
                if link != p["url"]:
                    inlinks[link] = inlinks.get(link, 0) + 1
        for p in results.values():
            p["inlinks"] = inlinks.get(p["url"], 0)
            p["depth"] = depth_of.get(p["url"], p["depth"])
        res.site["unvisited_queue"] = len(queue)
        res.pages = list(results.values())
        return res


def parse_html(url: str, html: str, x_robots: str = "") -> dict:
    soup = BeautifulSoup(html, "lxml")
    out: dict = {}
    # Structured data (before stripping scripts)
    types: list[str] = []
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        stack = [data]
        while stack:
            d = stack.pop()
            if isinstance(d, list):
                stack.extend(d)
            elif isinstance(d, dict):
                t = d.get("@type")
                if isinstance(t, list):
                    types.extend(str(x) for x in t)
                elif t:
                    types.append(str(t))
                stack.extend(v for v in d.values() if isinstance(v, (dict, list)))
    out["schema_types"] = sorted(set(types))
    # NAP hints from structured data / tel links
    out["phones"] = sorted({a["href"][4:].strip() for a in soup.select('a[href^="tel:"]')})[:5]

    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    out["title"] = title
    md = soup.find("meta", attrs={"name": re.compile("^description$", re.I)})
    out["meta_description"] = (md.get("content") or "").strip() if md else ""
    rm = soup.find("meta", attrs={"name": re.compile("^robots$", re.I)})
    robots = ((rm.get("content") or "") if rm else "").lower()
    out["robots_meta"] = (robots + (" " + x_robots.lower() if x_robots else "")).strip()
    out["indexable"] = "noindex" not in out["robots_meta"]
    can = soup.find("link", rel=lambda v: v and "canonical" in v)
    out["canonical"] = normalize(urljoin(url, can["href"])) if can and can.get("href") else ""
    out["has_viewport"] = bool(soup.find("meta", attrs={"name": "viewport"}))
    out["og_title"] = bool(soup.find("meta", attrs={"property": "og:title"}))
    html_tag = soup.find("html")
    out["lang"] = (html_tag.get("lang") or "") if html_tag else ""

    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("mailto:", "tel:", "javascript:", "#", "sms:")):
            continue
        links.append(normalize(urljoin(url, href)))
    out["links_out"] = sorted(set(links))

    imgs = soup.find_all("img")
    out["images_total"] = len(imgs)
    out["images_missing_alt"] = sum(1 for i in imgs if not (i.get("alt") or "").strip())

    for t in soup(["script", "style", "noscript", "svg", "iframe"]):
        t.decompose()
    out["h1"] = [h.get_text(" ", strip=True) for h in soup.find_all("h1")][:5]
    out["h2"] = [h.get_text(" ", strip=True) for h in soup.find_all("h2")][:25]
    body = soup.body or soup
    # Main content: drop nav/header/footer for word count, keep for NAP detection
    full_text = re.sub(r"\s+", " ", body.get_text(" ", strip=True))
    out["has_address_text"] = bool(re.search(r"\b\d{3,6}\s+\w+.*\b(st|street|ave|avenue|blvd|rd|road|dr|drive|suite|ste)\b", full_text, re.I))
    for t in body.find_all(["nav", "header", "footer"]):
        t.decompose()
    text = re.sub(r"\s+", " ", body.get_text(" ", strip=True))
    out["word_count"] = len(text.split())
    out["text"] = text[:TEXT_LIMIT]
    out["content_hash"] = hashlib.sha256(text.encode()).hexdigest()
    return out
