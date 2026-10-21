"""Cannibalization guard: detect when a proposed post or page covers the same
search as content the site already has (live pages, blog posts, planned ideas)."""

import re

STOP = set("""a an and are as at be best by can do does for from get how i in is it its me my near of on or our
than that the their this to vs versus what when where which who why will with you your much long guide complete
ultimate tips top ways things should need know about""".split())

DUPLICATE = 0.75  # same search: don't create
SIMILAR = 0.5  # related: link to it and differentiate


def tokens(text: str, extra_stop: set[str] = frozenset()) -> set[str]:
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    out = set()
    for w in words:
        if w in STOP or w in extra_stop or len(w) < 2:
            continue
        out.add(w[:-1] if len(w) > 4 and w.endswith("s") and not w.endswith("ss") else w)
    return out


def similarity(new: set[str], existing: set[str]) -> float:
    """How much of a proposed topic an existing piece already covers (1.0 = same topic)."""
    if not new or not existing:
        return 0.0
    shared = len(new & existing)
    if shared < 2 and len(new) > 1:
        return 0.0
    return shared / len(new)


def brand_words(c) -> set[str]:
    name = (c.profile or {}).get("business_name") or c.name or ""
    return tokens(name) | {"medspa", "spa", "med", "clinic"}


def inventory(c, exclude_post_id: int | None = None) -> list[dict]:
    """Everything the site has or has planned: [{kind, label, url, texts, keywords}]."""
    by_page: dict[str, list[str]] = {}
    for k in c.keywords:
        if k.target_url:
            by_page.setdefault(k.target_url, []).append(k.keyword)
    items = []
    for p in c.pages:
        if p.status_code != 200 or not p.indexable or (p.extra or {}).get("non_html"):
            continue
        items.append({"kind": "page", "label": p.title or p.url, "url": p.url,
                      "texts": [p.title or "", *(p.h1 or [])[:1]], "keywords": by_page.get(p.url, [])})
    for b in c.blog_posts:
        if b.id == exclude_post_id or b.status == "rejected":
            continue
        items.append({"kind": f"blog post ({b.status})", "label": b.title, "url": f"/blog/{b.id}",
                      "texts": [b.title], "keywords": [b.target_keyword] if b.target_keyword else []})
    for r in c.recommendations:
        if r.category == "new_page" and r.status in ("open", "approved"):
            items.append({"kind": "planned page", "label": r.title, "url": f"/recommendations/{r.id}",
                          "texts": [r.title], "keywords": [r.keyword] if r.keyword else []})
    return items


def matches(c, keyword: str, title: str = "", items: list[dict] | None = None,
            threshold: float = SIMILAR) -> list[dict]:
    """Existing content that targets the same search, best match first: [{..item, score}]."""
    stop = brand_words(c)
    kw_t, title_t = tokens(keyword, stop), tokens(title, stop)
    out = []
    for it in items if items is not None else inventory(c):
        score = 0.0
        for k in it["keywords"]:
            score = max(score, similarity(kw_t, tokens(k, stop)))  # keyword vs keyword is the strongest signal
        for t in it["texts"]:
            s = max(similarity(kw_t, tokens(t, stop)), similarity(title_t, tokens(t, stop)) * 0.9)
            score = max(score, s)
        if score >= threshold:
            out.append({**it, "score": round(score, 2)})
    return sorted(out, key=lambda x: -x["score"])


def prompt_inventory(c, limit: int = 250) -> list[dict]:
    """Compact content inventory to give the AI, so it proposes only new topics."""
    rows = []
    for it in inventory(c):
        rows.append({"type": it["kind"], "title": it["label"][:120], "url": it["url"] if it["kind"] == "page" else "",
                     "targets": it["keywords"][:3]})
    return rows[:limit]
