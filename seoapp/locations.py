"""A client's physical locations (from the business profile) and the
per-location Google Business Profile data and rank-check settings."""

import re

from . import sources

GBP_KEYS = {"profile_url", "primary_category", "secondary_categories", "business_name", "address", "phone",
            "website_link", "booking_link", "hours", "description", "services", "review_count", "rating",
            "reviews_last_30_days", "review_response_rate", "photo_count", "posts_last_30_days", "qna_count",
            "attributes", "notes", "_audited", "_maps_competitors"}


def key_for(loc: dict) -> str:
    name = loc.get("city") or loc.get("name") or "main"
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "main"


def all_for(c) -> list[dict]:
    """Locations with a stable key; the first is the primary. Always at least one."""
    out, seen = [], set()
    for loc in (c.profile or {}).get("locations") or []:
        k = key_for(loc)
        if k in seen:
            k = f"{k}-{len(seen) + 1}"
        seen.add(k)
        out.append({**loc, "key": k, "label": loc.get("city") or loc.get("name") or k,
                    "rank_location": loc.get("dataforseo_location") or "",
                    "service_areas": loc.get("service_areas") or []})
    if not out:
        out.append({"key": "main", "label": "Main location", "name": c.name, "city": "", "address": "",
                    "phone": "", "rank_location": c.rank_location or "", "service_areas": []})
    if out and not out[0]["rank_location"]:
        out[0]["rank_location"] = c.rank_location or ""
    return out


def get(c, key: str) -> dict:
    locs = all_for(c)
    return next((l for l in locs if l["key"] == key), locs[0])


# -- Google Business Profile per location ------------------------------------
def gbp_all(c) -> dict[str, dict]:
    """{location key: profile fields}. Older single-profile data belongs to the primary location."""
    data = dict(c.gbp or {})
    locs = all_for(c)
    legacy = {k: v for k, v in data.items() if k in GBP_KEYS}
    out = {k: v for k, v in data.items() if isinstance(v, dict) and k not in GBP_KEYS}
    if legacy:
        out.setdefault(locs[0]["key"], {}).update(legacy)
    return out


def gbp_for(c, key: str) -> dict:
    return gbp_all(c).get(key, {})


def set_gbp(c, key: str, fields: dict) -> None:
    allg = gbp_all(c)
    allg[key] = fields
    c.gbp = allg  # rewritten in the per-location format


def has_any_gbp(c) -> bool:
    return any(v for v in gbp_all(c).values())


# -- keyword <-> location -----------------------------------------------------
def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", (s or "").lower()).strip()


def for_keyword(c, k) -> list[dict]:
    """Locations a keyword belongs to: the one whose city or service area it names, else all of them."""
    locs = all_for(c)
    text = " " + _norm(k.keyword + " " + (k.location or "")) + " "
    hits = [l for l in locs if any(f" {_norm(a)} " in text for a in [l.get("city"), l.get("name")] + l["service_areas"] if a)]
    return hits or locs


def city_in_keyword(c, k) -> str:
    """The city a keyword names (its own location field, or a known city/service area in its text)."""
    if k.location:
        return k.location
    text = " " + _norm(k.keyword) + " "
    for l in all_for(c):
        for a in [l.get("city")] + l["service_areas"]:
            if a and f" {_norm(a)} " in text:
                return a
    return ""


def rank_location_for(c, k) -> tuple[str, list[dict]]:
    """(DataForSEO location to search from, locations whose Maps rank to record).

    A keyword naming a city is searched from that city; a generic local keyword
    ("botox near me") is searched from every location separately (handled by the caller)."""
    locs = for_keyword(c, k)
    city = city_in_keyword(c, k)
    if city:
        state_country = ",".join((locs[0]["rank_location"] or "").split(",")[1:])
        place = f"{city},{state_country}" if state_country else locs[0]["rank_location"]
        return place, locs[:1]
    return locs[0]["rank_location"], locs


def matches_business(c, loc: dict, title: str, domain: str) -> bool:
    own = sources.domain_of(c.url)
    names = {(c.profile or {}).get("business_name", "").lower(), c.name.lower()}
    return bool(domain and (domain == own or domain.endswith("." + own))) or any(n and n in title.lower() for n in names)
