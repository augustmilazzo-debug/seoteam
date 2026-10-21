"""Technical and on-page checks over a crawl. Each finding becomes one
recommendation (grouped across pages so the list stays actionable)."""

import math
from collections import defaultdict


def _urls(items, limit=25) -> str:
    items = list(items)
    lines = [f"- {u}" for u in items[:limit]]
    if len(items) > limit:
        lines.append(f"- ...and {len(items) - limit} more (see the Pages tab)")
    return "\n".join(lines)


def rec(code, title, details, impact, effort, confidence=0.9, page_url="", category="technical"):
    return {
        "fingerprint": f"tech:{code}" + (f":{page_url}" if page_url else ""),
        "category": category,
        "title": title,
        "details": details,
        "page_url": page_url,
        "impact": impact,
        "effort": effort,
        "confidence": confidence,
    }


def run_checks(site: dict, pages: list[dict], profile: dict | None = None) -> list[dict]:
    out: list[dict] = []
    home = site.get("final_home_url") or site.get("start_url", "")

    if not site.get("home_status") or site.get("home_status", 0) >= 400:
        out.append(rec("home_down", "Homepage is not loading for search engines",
                       f"The homepage returned status {site.get('home_status')}. Nothing else matters until this is fixed.",
                       10, 2, 1.0))
        return out
    if not site.get("https"):
        out.append(rec("no_https", "Site is not served over HTTPS",
                       "Install an SSL certificate and redirect all http:// URLs to https://. Google uses HTTPS as a ranking signal and browsers show 'Not secure'.",
                       9, 3))
    elif site.get("http_redirects_to_https") is False:
        out.append(rec("http_no_redirect", "http:// version does not redirect to https://",
                       "Add a site-wide 301 redirect from http:// to https:// so Google consolidates signals on one version.",
                       7, 2))
    if site.get("robots_blocks_all"):
        out.append(rec("robots_block", "robots.txt is blocking Google from the whole site",
                       "robots.txt contains `Disallow: /`. Remove it (in WordPress: Settings > Reading > uncheck 'Discourage search engines').",
                       10, 1, 1.0))
    if not site.get("robots_txt"):
        out.append(rec("no_robots", "No robots.txt file",
                       "Add a robots.txt at the site root that allows crawling and lists the sitemap URL.", 3, 1))
    if not site.get("sitemaps"):
        out.append(rec("no_sitemap", "No XML sitemap found",
                       "Create an XML sitemap (Yoast/RankMath on WordPress, built in on Wix/Squarespace/Webflow), reference it in robots.txt, and submit it in Google Search Console.",
                       6, 2))

    html = [p for p in pages if p.get("status_code") == 200 and p.get("title") is not None and not p.get("non_html")]
    by_url = {p["url"]: p for p in pages}
    indexable = [p for p in html if p.get("indexable", True)
                 and (not p.get("canonical") or p["canonical"] == p["url"])]

    # Broken internal links and redirects
    broken = defaultdict(set)
    redirected = defaultdict(set)
    for p in html:
        for link in p.get("links_out", []):
            t = by_url.get(link)
            if not t:
                continue
            if t.get("status_code", 0) >= 400 or t.get("status_code") == 0:
                broken[link].add(p["url"])
            elif t.get("redirect_to"):
                redirected[link].add(p["url"])
    if broken:
        lines = [f"- {u} (status {by_url[u].get('status_code')}) linked from {len(src)} page(s), e.g. {sorted(src)[0]}"
                 for u, src in sorted(broken.items(), key=lambda kv: -len(kv[1]))[:25]]
        out.append(rec("broken_links", f"Fix {len(broken)} broken internal link target(s)",
                       "These internal links lead to error pages. Update each link to the correct live page, or 301-redirect the dead URL to its closest replacement.\n\n" + "\n".join(lines),
                       7, 3))
    if len(redirected) >= 3:
        lines = [f"- {u} -> {by_url[u]['redirect_to']}" for u in sorted(redirected)[:25]]
        out.append(rec("internal_redirects", f"Update {len(redirected)} internal links that point to redirects",
                       "Link directly to the final URL so link equity is not lost through redirect hops.\n\n" + "\n".join(lines),
                       3, 3, 0.8))
    chains = [p for p in pages if p.get("redirect_to") and by_url.get(p["redirect_to"], {}).get("redirect_to")]
    if chains:
        out.append(rec("redirect_chains", f"{len(chains)} redirect chain(s)",
                       "These URLs redirect more than once. Point each one straight at the final destination.\n\n" + _urls(p["url"] for p in chains),
                       4, 2))

    # Indexing
    noindex = [p for p in html if not p.get("indexable", True)]
    in_sitemap = set(site.get("sitemap_urls", []))
    bad_noindex = [p for p in noindex if p["url"] in in_sitemap or p["url"] == home or p.get("inlinks", 0) >= 3]
    if bad_noindex:
        impact = 10 if any(p["url"] == home for p in bad_noindex) else 8
        out.append(rec("noindex_important", f"{len(bad_noindex)} important page(s) are set to noindex",
                       "These pages are linked across the site or listed in the sitemap but tell Google not to index them. If they should rank, remove the noindex tag.\n\n" + _urls(p["url"] for p in bad_noindex),
                       impact, 1, 0.85))
    canon_other = [p for p in html if p.get("canonical") and p["canonical"] != p["url"]
                   and p["canonical"].rstrip("/") != p["url"].rstrip("/")]
    if canon_other:
        out.append(rec("canonical_other", f"{len(canon_other)} page(s) canonicalize to a different URL",
                       "Check these are intentional duplicates. If a page should rank on its own, set its canonical to itself.\n\n" +
                       "\n".join(f"- {p['url']} -> {p['canonical']}" for p in canon_other[:25]), 4, 2, 0.6))
    no_canon = [p for p in indexable if not p.get("canonical")]
    if len(no_canon) >= max(3, len(indexable) // 3):
        out.append(rec("no_canonical", f"{len(no_canon)} pages have no canonical tag",
                       "Add self-referencing canonical tags (any SEO plugin does this automatically) to prevent duplicate-URL issues.\n\n" + _urls(p["url"] for p in no_canon),
                       3, 2, 0.7))
    if in_sitemap:
        crawled = {p["url"] for p in pages}
        sm_bad = [u for u in in_sitemap if u in by_url and (by_url[u].get("status_code") != 200 or not by_url[u].get("indexable", True))]
        if sm_bad:
            out.append(rec("sitemap_bad_urls", f"Sitemap lists {len(sm_bad)} URL(s) that are redirects, errors or noindex",
                           "A sitemap should only list live, indexable pages. Remove these from the sitemap.\n\n" + _urls(sm_bad), 4, 2))
        missing = [p["url"] for p in indexable if p["url"] not in in_sitemap and p["url"].rstrip("/") + "/" not in in_sitemap]
        if len(missing) >= 3 and crawled:
            out.append(rec("not_in_sitemap", f"{len(missing)} indexable page(s) missing from the sitemap",
                           "Add these pages to the XML sitemap so Google discovers and recrawls them faster.\n\n" + _urls(missing), 3, 2, 0.7))

    # Titles and descriptions
    def dupes(field):
        groups = defaultdict(list)
        for p in indexable:
            v = (p.get(field) or "").strip().lower()
            if v:
                groups[v].append(p["url"])
        return {k: v for k, v in groups.items() if len(v) > 1}

    missing_title = [p["url"] for p in indexable if not (p.get("title") or "").strip()]
    if missing_title:
        out.append(rec("missing_title", f"{len(missing_title)} page(s) have no title tag",
                       "Every page needs a unique title (50-60 characters) with its main keyword and city near the front.\n\n" + _urls(missing_title), 8, 2, category="on_page"))
    dt = dupes("title")
    if dt:
        n = sum(len(v) for v in dt.values())
        lines = [f"- \"{k[:80]}\" used on {len(v)} pages: " + ", ".join(v[:4]) for k, v in list(dt.items())[:15]]
        out.append(rec("duplicate_title", f"{n} pages share duplicate titles",
                       "Duplicate titles make pages compete with each other. Give each page a unique title targeting its own keyword.\n\n" + "\n".join(lines), 6, 3, category="on_page"))
    long_t = [p for p in indexable if len(p.get("title") or "") > 65]
    if long_t:
        out.append(rec("long_title", f"{len(long_t)} title(s) are too long and get cut off in Google",
                       "Keep titles under ~60 characters so the keyword and brand are visible.\n\n" +
                       "\n".join(f"- {p['url']} ({len(p['title'])} chars): {p['title'][:90]}" for p in long_t[:25]), 3, 2, 0.7, category="on_page"))
    short_t = [p for p in indexable if 0 < len(p.get("title") or "") < 25]
    if short_t:
        out.append(rec("short_title", f"{len(short_t)} title(s) are too short to target a keyword",
                       "Expand to 50-60 characters: service + city + brand, e.g. 'Lip Filler in Newport Beach | Brand'.\n\n" +
                       "\n".join(f"- {p['url']}: \"{p['title']}\"" for p in short_t[:25]), 5, 2, 0.8, category="on_page"))
    missing_md = [p["url"] for p in indexable if not (p.get("meta_description") or "").strip()]
    if missing_md:
        out.append(rec("missing_meta", f"{len(missing_md)} page(s) have no meta description",
                       "Write a 140-155 character description per page with the keyword, a benefit, and a call to action. It drives click-through rate from Google.\n\n" + _urls(missing_md),
                       4, 3, 0.8, category="on_page"))
    dm = dupes("meta_description")
    if dm:
        n = sum(len(v) for v in dm.values())
        out.append(rec("duplicate_meta", f"{n} pages share duplicate meta descriptions",
                       "\n".join(f"- used on: " + ", ".join(v[:5]) for v in list(dm.values())[:15]), 3, 3, 0.7, category="on_page"))

    # Headings and content
    no_h1 = [p["url"] for p in indexable if not p.get("h1")]
    if no_h1:
        out.append(rec("missing_h1", f"{len(no_h1)} page(s) have no H1 heading",
                       "Each page needs exactly one H1 that states the page's topic with its main keyword.\n\n" + _urls(no_h1), 6, 2, 0.85, category="on_page"))
    multi_h1 = [p["url"] for p in indexable if len(p.get("h1") or []) > 1]
    if multi_h1:
        out.append(rec("multiple_h1", f"{len(multi_h1)} page(s) have more than one H1",
                       "Keep one H1 per page and change the others to H2.\n\n" + _urls(multi_h1), 2, 2, 0.6, category="on_page"))
    thin = [p for p in indexable if p.get("word_count", 0) < 300 and p["url"] != home
            and not any(x in p["url"] for x in ("/contact", "/book", "/thank", "/privacy", "/terms", "/cart", "/login"))]
    if thin:
        out.append(rec("thin_content", f"{len(thin)} page(s) have thin content (under 300 words)",
                       "Service pages that rank well usually have 800-1,500 words: what it is, who it's for, how it works, results, downtime, pricing guidance, provider credentials, FAQs. Expand these or merge them into stronger pages.\n\n" +
                       "\n".join(f"- {p['url']} ({p.get('word_count', 0)} words)" for p in sorted(thin, key=lambda x: x.get('word_count', 0))[:25]),
                       6, 6, 0.75, category="content"))

    # Internal linking
    orphans = [p["url"] for p in indexable if p.get("inlinks", 0) == 0 and p["url"] != home]
    if orphans:
        out.append(rec("orphan_pages", f"{len(orphans)} orphan page(s) with no internal links pointing to them",
                       "Google treats pages nobody links to as unimportant. Link to each from a relevant service page, the menu, or a blog post.\n\n" + _urls(orphans), 5, 3, 0.8))
    deep = [p["url"] for p in indexable if 3 < p.get("depth", 0) < 99]
    if deep:
        out.append(rec("deep_pages", f"{len(deep)} page(s) are more than 3 clicks from the homepage",
                       "Important pages should be reachable within 3 clicks. Add links from the menu, homepage or hub pages.\n\n" + _urls(deep), 3, 3, 0.6))

    # Images, mobile, misc
    alt_pages = [p for p in indexable if p.get("images_missing_alt", 0) > 0]
    total_alt = sum(p["images_missing_alt"] for p in alt_pages)
    if total_alt >= 5:
        out.append(rec("img_alt", f"{total_alt} images are missing alt text",
                       "Add short descriptive alt text (e.g. 'lip filler before and after, Newport Beach'). Helps image search and accessibility.\n\n" +
                       "\n".join(f"- {p['url']} ({p['images_missing_alt']} images)" for p in sorted(alt_pages, key=lambda x: -x['images_missing_alt'])[:25]),
                       3, 3, 0.7, category="on_page"))
    no_vp = [p["url"] for p in indexable if not p.get("has_viewport", True)]
    if no_vp:
        out.append(rec("no_viewport", f"{len(no_vp)} page(s) are missing the mobile viewport tag",
                       "Google indexes the mobile version first. Add `<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">`.\n\n" + _urls(no_vp), 7, 2))
    slow = [p for p in indexable if p.get("response_ms", 0) > 1500]
    if len(slow) >= max(2, len(indexable) // 5):
        out.append(rec("slow_server", f"{len(slow)} page(s) take over 1.5s for the server to respond",
                       "Slow server response hurts rankings and conversions. Enable page caching, a CDN, or better hosting.\n\n" +
                       "\n".join(f"- {p['url']} ({p['response_ms']} ms)" for p in sorted(slow, key=lambda x: -x['response_ms'])[:25]), 5, 5, 0.6))
    if home in by_url and not by_url[home].get("lang"):
        out.append(rec("no_lang", "Homepage is missing the html lang attribute",
                       "Add `lang=\"en\"` to the `<html>` tag.", 1, 1, 0.5, category="on_page"))
    no_og = [p for p in indexable if not p.get("og_title")]
    if len(no_og) > len(indexable) // 2 and indexable:
        out.append(rec("no_og", "Most pages are missing Open Graph tags",
                       "Add og:title, og:description and og:image (any SEO plugin) so links shared on social look professional.", 2, 2, 0.5, category="on_page"))

    # Structured data and local signals
    all_types = {t for p in html for t in p.get("schema_types", [])}
    local_types = {"LocalBusiness", "MedicalBusiness", "MedicalClinic", "HealthAndBeautyBusiness", "DaySpa",
                   "BeautySalon", "Dentist", "Physician", "MedicalSpa", "Organization", "ProfessionalService"}
    home_types = set(by_url.get(home, {}).get("schema_types", []))
    if not (home_types & (local_types - {"Organization"})):
        out.append(rec("no_local_schema", "No LocalBusiness / MedicalBusiness schema on the homepage",
                       "Add LocalBusiness (for a medspa: `MedicalBusiness` + `HealthAndBeautyBusiness`) JSON-LD with name, address, phone, geo coordinates, hours, priceRange, sameAs (Google Business Profile, Instagram, Yelp) and aggregateRating if allowed. Use one block per location on each location page. This strongly supports Maps rankings.",
                       7, 3, 0.85))
    if "FAQPage" not in all_types:
        out.append(rec("no_faq_schema", "No FAQ content marked up with FAQPage schema",
                       "Add an FAQ section to each service page (questions patients actually ask: cost, pain, downtime, how long results last) and mark it up with FAQPage schema. It helps with long-tail queries and AI answers.",
                       4, 4, 0.6, category="content"))
    if "BreadcrumbList" not in all_types and len(indexable) > 10:
        out.append(rec("no_breadcrumbs", "No breadcrumb schema",
                       "Enable breadcrumbs in your SEO plugin so Google shows a clean path in results.", 2, 1, 0.6))
    if home in by_url:
        hx = by_url[home].get("extra", {}) or {}
        if not hx.get("phones"):
            out.append(rec("no_click_to_call", "Homepage has no click-to-call phone link",
                           "Add the phone number as a `tel:` link in the header. It's a conversion driver and reinforces your name/address/phone (NAP) for local SEO.",
                           4, 1, 0.8, category="local"))
        if not hx.get("has_address_text"):
            out.append(rec("no_address", "Business address not visible on the homepage",
                           "Show the full address (matching Google Business Profile exactly) in the footer of every page.",
                           5, 1, 0.75, category="local"))

    # PageSpeed (only for pages measured)
    for p in html:
        ps = p.get("pagespeed") or {}
        perf = ps.get("performance")
        if perf is None:
            continue
        if perf < 50 or (ps.get("lcp_ms") or 0) > 4000:
            out.append(rec("slow_mobile", f"Poor mobile speed score ({perf}/100)",
                           f"Mobile PageSpeed for {p['url']}: performance {perf}, LCP {ps.get('lcp_ms')} ms, CLS {ps.get('cls')}.\n\n"
                           "Typical fixes: compress and resize images (WebP), lazy-load below-the-fold images, remove unused plugins/scripts, enable caching and a CDN.\n\n"
                           "Top opportunities from Google:\n" + "\n".join(f"- {o}" for o in ps.get("opportunities", [])[:6]),
                           6, 5, 0.7, page_url=p["url"]))
    return out


def health_score(recs: list) -> int:
    """0-100 site health from open issues the crawler detected (not AI ideas)."""
    penalty = 0.0
    for r in recs:
        if r.fingerprint.startswith("tech:") and r.status in ("open", "approved"):
            penalty += {"critical": 15, "high": 6, "medium": 3, "low": 1}.get(r.level, 2)
    return round(100 * math.exp(-penalty / 80))
