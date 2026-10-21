"""Send approved blog posts to the client's website builder.

WordPress, Webflow and Shopify have APIs for blog posts, so posts are sent as
drafts. Wix, Squarespace and GoDaddy don't offer one for blog posts, so the app
shows copy-ready pieces and platform-specific steps instead.
"""

import html
import re

import requests

from . import blog

PLATFORMS = {
    "WordPress": {"api": True, "fields": [
        ("url", "Site address", "text", "https://example.com"),
        ("user", "WordPress username", "text", ""),
        ("app_password", "Application Password", "secret", "xxxx xxxx xxxx xxxx xxxx xxxx")],
        "help": "In WordPress: Users → Profile (an Editor or Administrator account) → Application Passwords → name it "
                "\"SEO Command Center\" → Add New Application Password."},
    "Webflow": {"api": True, "fields": [
        ("token", "Site API token", "secret", ""),
        ("collection_id", "Blog collection ID (optional: found automatically)", "text", "")],
        "help": "In Webflow: Site settings → Apps & Integrations → API access → Generate API token with "
                "CMS read and write access."},
    "Shopify": {"api": True, "fields": [
        ("shop", "Store address", "text", "your-store.myshopify.com"),
        ("token", "Admin API access token", "secret", "shpat_…"),
        ("blog_id", "Blog ID (optional: found automatically)", "text", "")],
        "help": "In Shopify admin: Settings → Apps and sales channels → Develop apps → Create an app → Admin API "
                "scopes: read_content and write_content → Install → copy the Admin API access token."},
    "Wix": {"api": False, "steps": [
        "In the Wix dashboard open Blog → Create New Post.",
        "Paste the title, then click into the body and paste the formatted article (Copy formatted article).",
        "Post settings → SEO: paste the SEO title, meta description and URL slug.",
        "Post settings → SEO → Advanced → Structured data markup → Add new markup → paste the schema.",
        "Add a cover image, then Publish (or Save as draft).",
    ]},
    "Squarespace": {"api": False, "steps": [
        "Open the blog page → + (new post).",
        "Paste the title, then paste the formatted article into the text block.",
        "Post settings → SEO: paste the SEO title and description; Options → URL slug.",
        "Post settings → Advanced → Post blog item code injection: paste the schema wrapped in "
        "<script type=\"application/ld+json\"> … </script> (Copy schema does this for you).",
        "Add a thumbnail image, then Publish.",
    ]},
    "GoDaddy": {"api": False, "steps": [
        "Website builder → Blog → Add post.",
        "Paste the title, then paste the formatted article.",
        "Post settings → SEO: paste the SEO title, description and URL.",
        "Add a featured image, then Publish.",
    ]},
    "Other": {"api": False, "steps": [
        "Create a new blog post in the site's editor.",
        "Paste the formatted article, or the HTML (Copy HTML) if the editor has an HTML/code mode.",
        "Set the SEO title, meta description and URL slug in the post's SEO settings.",
        "Add the schema in the page's custom code / head section if the platform allows it.",
    ]},
}


def platform_for(c) -> str:
    cms = (c.cms or "").strip()
    return cms if cms in PLATFORMS else "Other"


def connected(c) -> bool:
    conf = c.wp or {}
    p = platform_for(c)
    if p == "WordPress":
        return bool(conf.get("url") and conf.get("user") and conf.get("app_password"))
    if p == "Webflow":
        return bool(conf.get("token"))
    if p == "Shopify":
        return bool(conf.get("shop") and conf.get("token"))
    return False


class PublishError(Exception):
    pass


def _strip_script(content: str) -> str:
    """Builders other than WordPress reject <script> in post bodies; schema is added separately."""
    return re.sub(r"<!-- wp:html -->.*?<!-- /wp:html -->", "", content, flags=re.S)


# -- Webflow -------------------------------------------------------------------
def _wf(conf, method, path, **kw):
    try:
        r = requests.request(method, f"https://api.webflow.com/v2/{path}", timeout=60,
                             headers={"Authorization": f"Bearer {conf.get('token', '')}", "accept": "application/json"}, **kw)
    except requests.RequestException as e:
        raise PublishError(f"Could not reach Webflow: {e}") from e
    if r.status_code in (401, 403):
        raise PublishError("Webflow rejected the token. Generate a site API token with CMS read and write access.")
    if r.status_code >= 400:
        raise PublishError(f"Webflow error {r.status_code}: {r.text[:300]}")
    return r.json() if r.content else {}


def _wf_collection(conf) -> tuple[str, list]:
    cid = conf.get("collection_id")
    if not cid:
        sites = _wf(conf, "GET", "sites").get("sites", [])
        if not sites:
            raise PublishError("This Webflow token can't see any site.")
        cols = _wf(conf, "GET", f"sites/{sites[0]['id']}/collections").get("collections", [])
        match = [x for x in cols if re.search(r"blog|post|article|news", (x.get("displayName", "") + x.get("slug", "")), re.I)]
        if not match:
            raise PublishError("No blog collection found in Webflow. Enter the collection ID in Settings.")
        cid = match[0]["id"]
    return cid, _wf(conf, "GET", f"collections/{cid}").get("fields", [])


def webflow_push(conf, post, profile, ctx) -> tuple[str, str, dict]:
    cid, fields = _wf_collection(conf)
    by_type = {}
    for f in fields:
        by_type.setdefault(f.get("type"), []).append(f)
    rich = next((f["slug"] for f in by_type.get("RichText", [])), None)
    if not rich:
        raise PublishError("The Webflow blog collection has no rich text field for the article body.")
    plain = [f for f in by_type.get("PlainText", []) if f["slug"] not in ("name", "slug")]
    summary = next((f["slug"] for f in plain if re.search(r"summary|excerpt|intro|description", f["slug"], re.I)), None)
    data = {"name": post.title, "slug": post.slug, rich: _strip_script(blog.full_html(post, profile, ctx, include_schema=False))}
    if summary:
        data[summary] = post.excerpt or post.meta_description
    meta_t = next((f["slug"] for f in plain if re.search(r"meta.?title|seo.?title", f["slug"], re.I)), None)
    meta_d = next((f["slug"] for f in plain if re.search(r"meta.?desc|seo.?desc", f["slug"], re.I)), None)
    if meta_t:
        data[meta_t] = post.meta_title
    if meta_d:
        data[meta_d] = post.meta_description
    body = {"isArchived": False, "isDraft": True, "fieldData": data}
    if post.cms_id:
        item = _wf(conf, "PATCH", f"collections/{cid}/items/{post.cms_id}", json=body)
    else:
        item = _wf(conf, "POST", f"collections/{cid}/items", json=body)
    return str(item.get("id", post.cms_id)), "https://webflow.com/dashboard", {
        "seo_fields": bool(meta_t and meta_d), "schema": None, "category": False,
        "note": "Webflow doesn't allow schema code inside the post body. Add the schema once to the blog "
                "template's custom code (or paste it per post if your template has a custom-code field)."}


# -- Shopify -------------------------------------------------------------------
SHOPIFY_API = "2024-10"


def _shop(conf, method, path, **kw):
    shop = re.sub(r"^https?://", "", (conf.get("shop") or "")).strip("/")
    if not shop:
        raise PublishError("Shopify store address is not set.")
    try:
        r = requests.request(method, f"https://{shop}/admin/api/{SHOPIFY_API}/{path}", timeout=60,
                             headers={"X-Shopify-Access-Token": conf.get("token", "")}, **kw)
    except requests.RequestException as e:
        raise PublishError(f"Could not reach Shopify: {e}") from e
    if r.status_code in (401, 403):
        raise PublishError("Shopify rejected the token. The app needs the read_content and write_content scopes.")
    if r.status_code >= 400:
        raise PublishError(f"Shopify error {r.status_code}: {r.text[:300]}")
    return shop, (r.json() if r.content else {})


def shopify_push(conf, post, profile, ctx) -> tuple[str, str, dict]:
    blog_id = conf.get("blog_id")
    if not blog_id:
        _, data = _shop(conf, "GET", "blogs.json")
        blogs = data.get("blogs", [])
        if not blogs:
            raise PublishError("This Shopify store has no blog yet. Create one under Online Store → Blog posts.")
        blog_id = next((b["id"] for b in blogs if re.search(r"blog|news", b.get("title", ""), re.I)), blogs[0]["id"])
    article = {
        "title": post.title, "handle": post.slug, "published": False,
        "body_html": _strip_script(blog.full_html(post, profile, ctx, include_schema=False)),
        "summary_html": f"<p>{html.escape(post.excerpt or post.meta_description)}</p>",
        "tags": post.category or "",
        "metafields": [
            {"namespace": "global", "key": "title_tag", "value": post.meta_title, "type": "single_line_text_field"},
            {"namespace": "global", "key": "description_tag", "value": post.meta_description, "type": "single_line_text_field"},
        ],
    }
    if post.cms_id:
        shop, data = _shop(conf, "PUT", f"blogs/{blog_id}/articles/{post.cms_id}.json", json={"article": article})
    else:
        shop, data = _shop(conf, "POST", f"blogs/{blog_id}/articles.json", json={"article": article})
    aid = str(data.get("article", {}).get("id", post.cms_id))
    return aid, f"https://{shop}/admin/articles/{aid}", {
        "seo_fields": True, "schema": None, "category": bool(post.category),
        "note": "Most Shopify themes already add article schema. To add the FAQ schema, paste it into the "
                "article template's custom code, or use an SEO app."}


def send(c, post) -> tuple[str, str, dict]:
    """Send the post as a draft to the client's platform: (id, edit url, checks)."""
    p = platform_for(c)
    ctx = blog.site_context(c)
    conf = c.wp or {}
    if p == "WordPress":
        try:
            return blog.wp_push(conf, post, c.profile or {}, ctx)
        except blog.WordPressError as e:
            raise PublishError(str(e)) from e
    if p == "Webflow":
        return webflow_push(conf, post, c.profile or {}, ctx)
    if p == "Shopify":
        return shopify_push(conf, post, c.profile or {}, ctx)
    raise PublishError(f"{p} has no API for blog posts; use the copy-and-paste steps.")


def test(c) -> str:
    p, conf = platform_for(c), c.wp or {}
    if p == "WordPress":
        try:
            return blog.wp_test(conf)
        except blog.WordPressError as e:
            raise PublishError(str(e)) from e
    if p == "Webflow":
        cid, fields = _wf_collection(conf)
        return f"Connected to Webflow. Blog collection found with {len(fields)} fields."
    if p == "Shopify":
        _, data = _shop(conf, "GET", "blogs.json")
        names = ", ".join(b.get("title", "") for b in data.get("blogs", [])) or "no blogs yet"
        return f"Connected to Shopify. Blogs: {names}."
    raise PublishError(f"{p} doesn't have a blog API; posts are published by copy and paste.")


def detect_platform(page_html: str, headers: dict) -> str:
    """Best guess at the website builder from the homepage."""
    h = page_html[:200000].lower()
    hk = " ".join(f"{k}:{v}" for k, v in headers.items()).lower()
    if "wp-content" in h or "wp-includes" in h or 'content="wordpress' in h:
        return "WordPress"
    if "data-wf-site" in h or "webflow" in hk or "assets.website-files.com" in h:
        return "Webflow"
    if "cdn.shopify.com" in h or "x-shopify" in hk:
        return "Shopify"
    if "static.wixstatic.com" in h or "x-wix" in hk or "wix.com" in h[:5000]:
        return "Wix"
    if "squarespace" in h or "static1.squarespace.com" in h:
        return "Squarespace"
    if "godaddy" in h or "img1.wsimg.com" in h:
        return "GoDaddy"
    return ""
