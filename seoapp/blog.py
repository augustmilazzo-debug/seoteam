"""Blog engine: render articles to publish-ready HTML, score them against an
SEO/AEO checklist, and push them to WordPress as drafts."""

import html
import json
import re

import requests
from markdown_it import MarkdownIt

_md = MarkdownIt("commonmark", {"html": False}).enable("table")
MARKER = re.compile(r"(?:<p>)?\[\[visual:(\d+)\]\](?:</p>)?")


def _e(s) -> str:
    return html.escape(str(s or ""), quote=True)


def render_visual(v: dict) -> str:
    kind = v.get("kind")
    cap = f"<figcaption>{_e(v.get('caption'))}</figcaption>" if v.get("caption") else ""
    title = f"<p><strong>{_e(v.get('title'))}</strong></p>" if v.get("title") else ""
    if kind == "table" and v.get("columns") and v.get("rows"):
        head = "".join(f"<th>{_e(c)}</th>" for c in v["columns"])
        body = "".join("<tr>" + "".join(f"<td>{_e(x)}</td>" for x in row) + "</tr>" for row in v["rows"])
        return (f'<figure class="wp-block-table seo-visual">{title}<table><thead><tr>{head}</tr></thead>'
                f"<tbody>{body}</tbody></table>{cap}</figure>")
    if kind == "bar_chart" and v.get("labels") and v.get("values"):
        top = max(v["values"]) or 1
        unit = _e(v.get("unit"))
        bars = "".join(
            f'<div style="display:flex;align-items:center;gap:8px;margin:6px 0;">'
            f'<span style="flex:0 0 32%;font-size:14px;">{_e(label)}</span>'
            f'<span style="flex:1;background:#eef1f6;border-radius:4px;">'
            f'<span style="display:block;width:{max(4, round(val / top * 100))}%;background:#4c6ef5;height:18px;border-radius:4px;"></span></span>'
            f'<span style="flex:0 0 auto;font-size:14px;"><strong>{val:g}</strong> {unit}</span></div>'
            for label, val in zip(v["labels"], v["values"]))
        return (f'<figure class="seo-visual" role="img" aria-label="{_e(v.get("alt_text"))}">{title}{bars}{cap}</figure>')
    if kind == "photo":
        return (f'<figure class="seo-visual seo-photo-slot" style="border:2px dashed #c5cbd6;padding:16px;'
                f'border-radius:8px;"><p><strong>Add photo:</strong> {_e(v.get("photo_brief"))}</p>'
                f'<p><em>Alt text:</em> {_e(v.get("alt_text"))}</p>{cap}</figure>')
    return ""


def body_html(post) -> str:
    out = _md.render(post.body or "")
    visuals = post.visuals or []

    def sub(m):
        i = int(m.group(1)) - 1
        return render_visual(visuals[i]) if 0 <= i < len(visuals) else ""
    return MARKER.sub(sub, out)


def faq_html(post) -> str:
    if not post.faqs:
        return ""
    items = "".join(f"<h3>{_e(f['question'])}</h3><p>{_e(f['answer'])}</p>" for f in post.faqs)
    return f"<h2>Frequently asked questions</h2>{items}"


def schema_json(post, profile: dict, url: str = "") -> str:
    graph = [{
        "@type": "BlogPosting", "headline": post.title, "description": post.meta_description,
        "keywords": ", ".join([post.target_keyword] + list(post.secondary_keywords or [])),
        "publisher": {"@type": "Organization", "name": profile.get("business_name", "")},
    }]
    providers = profile.get("providers") or []
    if providers and profile.get("is_medical"):
        graph[0]["reviewedBy"] = {"@type": "Person", "name": providers[0]}
    if url:
        graph[0]["mainEntityOfPage"] = url
    if post.faqs:
        graph.append({"@type": "FAQPage", "mainEntity": [
            {"@type": "Question", "name": f["question"], "acceptedAnswer": {"@type": "Answer", "text": f["answer"]}}
            for f in post.faqs]})
    return json.dumps({"@context": "https://schema.org", "@graph": graph}, ensure_ascii=False, indent=1)


def full_html(post, profile: dict, include_schema: bool = True) -> str:
    """The post body as it should appear in the CMS (title and SEO fields are separate)."""
    parts = []
    if post.answer_summary:
        parts.append(f'<div class="seo-answer" style="background:#f3f6ff;border-left:4px solid #4c6ef5;'
                     f'padding:12px 16px;margin-bottom:16px;"><p><strong>Quick answer:</strong> {_e(post.answer_summary)}</p></div>')
    if post.key_takeaways:
        parts.append("<h2>Key takeaways</h2><ul>" + "".join(f"<li>{_e(t)}</li>" for t in post.key_takeaways) + "</ul>")
    parts.append(body_html(post))
    parts.append(faq_html(post))
    providers = profile.get("providers") or []
    if profile.get("is_medical") and providers:
        parts.append(f"<p><em>Medically reviewed by {_e(providers[0])}. Results vary; a consultation is "
                     f"required to determine whether a treatment is right for you.</em></p>")
    if post.references:
        parts.append("<p><small>Sources: " + "; ".join(_e(r) for r in post.references) + "</small></p>")
    if include_schema:
        parts.append(f'<!-- wp:html --><script type="application/ld+json">{schema_json(post, profile)}</script><!-- /wp:html -->')
    return "\n".join(p for p in parts if p)


# -- quality checklist --------------------------------------------------------
def checklist(post, site_host: str) -> list[tuple[bool, str]]:
    kw = (post.target_keyword or "").lower()
    text = re.sub(r"\[\[visual:\d+\]\]", "", post.body or "")
    words = len(text.split()) + sum(len((f.get("answer") or "").split()) for f in post.faqs or [])
    first100 = " ".join(text.split()[:100]).lower() + " " + (post.answer_summary or "").lower()
    links = re.findall(r"\]\((https?://[^)\s]+)\)", text)
    internal = [u for u in links if site_host and site_host in u]
    h2 = len(re.findall(r"(?m)^## ", text))
    kw_parts = [w for w in re.findall(r"[a-z0-9]+", kw) if len(w) > 2]

    def has_kw(s):
        s = (s or "").lower()
        return bool(kw) and (kw in s or all(w in s for w in kw_parts))
    return [
        (has_kw(post.title), "Target keyword in the title"),
        (has_kw(post.meta_title) and 30 <= len(post.meta_title) <= 62, f"SEO title has the keyword and is 30-60 characters ({len(post.meta_title)})"),
        (120 <= len(post.meta_description) <= 160, f"Meta description is 120-160 characters ({len(post.meta_description)})"),
        (has_kw(post.slug.replace("-", " ")), "Keyword in the URL slug"),
        (has_kw(first100), "Keyword in the opening answer or first 100 words"),
        (bool(post.answer_summary) and len(post.answer_summary.split()) <= 70, "Quotable quick answer at the top (for AI answers and featured snippets)"),
        (words >= 1000, f"In-depth: {words:,} words (aim for 1,000+)"),
        (h2 >= 4, f"{h2} question-style sections (H2)"),
        (len(internal) >= 2, f"{len(internal)} internal links (aim for 2+)"),
        (bool(post.supports_url) and any(post.supports_url.rstrip('/') in u for u in links), "Links to the service page it supports"),
        (len(post.faqs or []) >= 4, f"{len(post.faqs or [])} FAQs with FAQPage schema"),
        (len(post.visuals or []) >= 2, f"{len(post.visuals or [])} visuals (tables, charts or photos)"),
        (all(v.get("alt_text") for v in post.visuals or []), "Every visual has alt text"),
    ]


# -- WordPress ----------------------------------------------------------------
class WordPressError(Exception):
    pass


def _wp_base(wp: dict) -> str:
    url = (wp.get("url") or "").strip().rstrip("/")
    if not url:
        raise WordPressError("WordPress site address is not set")
    if not url.startswith("http"):
        url = "https://" + url
    return url


def _wp_request(wp: dict, method: str, path: str, **kwargs):
    base = _wp_base(wp)
    try:
        r = requests.request(method, f"{base}/wp-json/wp/v2/{path}", auth=(wp.get("user", ""), wp.get("app_password", "")),
                             timeout=60, **kwargs)
    except requests.RequestException as e:
        raise WordPressError(f"Could not reach {base}: {e}") from e
    if r.status_code in (401, 403):
        raise WordPressError("WordPress rejected the login. Check the username and Application Password, and that "
                             "the user can create posts (Editor or Administrator).")
    if r.status_code == 404:
        raise WordPressError("WordPress REST API not found at this address. Check the site address, and that no "
                             "security plugin blocks /wp-json.")
    if r.status_code >= 400:
        try:
            msg = r.json().get("message", r.text[:300])
        except ValueError:
            msg = r.text[:300]
        raise WordPressError(f"WordPress error {r.status_code}: {msg}")
    return r.json()


def wp_test(wp: dict) -> str:
    me = _wp_request(wp, "GET", "users/me", params={"context": "edit"})
    roles = ", ".join(me.get("roles", [])) or "unknown role"
    return f"Connected as {me.get('name') or me.get('slug')} ({roles})"


def wp_push(wp: dict, post, profile: dict) -> tuple[str, str]:
    """Create or update the post as a WordPress draft. Returns (post id, edit URL)."""
    data = {
        "title": post.title,
        "content": full_html(post, profile),
        "excerpt": post.excerpt or post.meta_description,
        "slug": post.slug,
        "status": "draft",
    }
    seo_meta = {  # filled in only if the site's SEO plugin exposes these fields to the API
        "_yoast_wpseo_title": post.meta_title, "_yoast_wpseo_metadesc": post.meta_description,
        "_yoast_wpseo_focuskw": post.target_keyword,
        "rank_math_title": post.meta_title, "rank_math_description": post.meta_description,
        "rank_math_focus_keyword": post.target_keyword,
    }
    path = f"posts/{post.cms_id}" if post.cms_id else "posts"
    try:
        res = _wp_request(wp, "POST", path, json={**data, "meta": seo_meta})
    except WordPressError as e:
        if "meta" not in str(e).lower():
            raise
        res = _wp_request(wp, "POST", path, json=data)
    pid = str(res["id"])
    return pid, f"{_wp_base(wp)}/wp-admin/post.php?post={pid}&action=edit"
