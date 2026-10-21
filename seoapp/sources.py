"""External data: Google PageSpeed, DataForSEO (volume, difficulty, organic and
Maps rankings) and Google Search Console. Each function returns None / empty
when its credentials are missing, so the rest of the system keeps working."""

import base64
import datetime as dt
import json
from urllib.parse import urlparse

import requests

from . import config

US = 2840
RANK_DEPTH = 30  # results checked per keyword: Google pages 1-3


# -- PageSpeed Insights -------------------------------------------------------
def pagespeed(url: str) -> dict | None:
    params = {"url": url, "strategy": "mobile", "category": "performance"}
    if config.PAGESPEED_API_KEY:
        params["key"] = config.PAGESPEED_API_KEY
    try:
        r = requests.get("https://www.googleapis.com/pagespeedonline/v5/runPagespeed", params=params, timeout=90)
        r.raise_for_status()
        lh = r.json()["lighthouseResult"]
    except (requests.RequestException, KeyError, ValueError):
        return None
    audits = lh.get("audits", {})
    opps = [a["title"] for a in audits.values()
            if (a.get("details") or {}).get("type") == "opportunity" and (a.get("score") or 1) < 0.9]
    return {
        "performance": round((lh["categories"]["performance"].get("score") or 0) * 100),
        "lcp_ms": round(audits.get("largest-contentful-paint", {}).get("numericValue") or 0),
        "cls": round(audits.get("cumulative-layout-shift", {}).get("numericValue") or 0, 3),
        "opportunities": opps[:8],
        "checked": dt.date.today().isoformat(),
    }


# -- DataForSEO ---------------------------------------------------------------
def dfs_enabled() -> bool:
    return bool(config.DATAFORSEO_LOGIN and config.DATAFORSEO_PASSWORD)


def _dfs(path: str, payload: list) -> list:
    r = requests.post(
        f"https://api.dataforseo.com/v3/{path}",
        auth=(config.DATAFORSEO_LOGIN, config.DATAFORSEO_PASSWORD),
        json=payload, timeout=120,
    )
    r.raise_for_status()
    data = r.json()
    out = []
    for task in data.get("tasks") or []:
        if task.get("status_code") != 20000:
            raise RuntimeError(f"DataForSEO {path}: {task.get('status_message')}")
        out.append(task.get("result") or [])
    return out


def keyword_metrics(keywords: list[str]) -> dict[str, dict]:
    """{keyword: {search_volume, cpc, difficulty}} for US Google."""
    if not dfs_enabled() or not keywords:
        return {}
    out: dict[str, dict] = {}
    for i in range(0, len(keywords), 700):
        chunk = keywords[i:i + 700]
        res = _dfs("keywords_data/google_ads/search_volume/live",
                   [{"keywords": chunk, "location_code": US, "language_code": "en"}])
        for item in (res[0] if res else []):
            out[item["keyword"].lower()] = {"search_volume": item.get("search_volume"), "cpc": item.get("cpc")}
        try:
            res = _dfs("dataforseo_labs/google/bulk_keyword_difficulty/live",
                       [{"keywords": chunk, "location_code": US, "language_code": "en"}])
            for block in (res[0] if res else []):
                for item in block.get("items") or []:
                    out.setdefault(item["keyword"].lower(), {})["difficulty"] = item.get("keyword_difficulty")
        except (RuntimeError, requests.RequestException):
            pass
    return out


def _loc(location_name: str) -> dict:
    return {"location_name": location_name} if location_name else {"location_code": US}


def organic_rank(keyword: str, domain: str, location_name: str) -> dict:
    """Position of `domain` in Google mobile results, plus the top competitors."""
    task = {"keyword": keyword, "language_code": "en", "device": "mobile", "depth": RANK_DEPTH, **_loc(location_name)}
    try:
        res = _dfs("serp/google/organic/live/advanced", [task])
    except RuntimeError:
        task.pop("location_name", None)
        task["location_code"] = US
        res = _dfs("serp/google/organic/live/advanced", [task])
    items = (res[0][0].get("items") if res and res[0] else None) or []
    position, url, top, local_pack = None, "", [], []
    for it in items:
        if it.get("type") == "organic":
            d = (it.get("domain") or "").removeprefix("www.")
            if len(top) < 5:
                top.append({"rank": it.get("rank_group"), "domain": d, "url": it.get("url"), "title": it.get("title")})
            if position is None and (d == domain or d.endswith("." + domain)):
                position, url = it.get("rank_group"), it.get("url", "")
        elif it.get("type") == "local_pack":
            local_pack.append((it.get("title") or "", (it.get("domain") or "").removeprefix("www.")))
    return {"position": position, "url": url, "top": top, "local_pack": local_pack}


def maps_rank(keyword: str, domain: str, business_name: str, location_name: str) -> int | None:
    task = {"keyword": keyword, "language_code": "en", "depth": 20, **_loc(location_name)}
    try:
        res = _dfs("serp/google/maps/live/advanced", [task])
    except RuntimeError:
        return None
    items = (res[0][0].get("items") if res and res[0] else None) or []
    name = business_name.lower().strip()
    for it in items:
        d = (it.get("domain") or "").removeprefix("www.")
        title = (it.get("title") or "").lower()
        if (d and (d == domain or d.endswith("." + domain))) or (name and name in title):
            return it.get("rank_group")
    return None


# -- Google Search Console ----------------------------------------------------
def _gsc_creds():
    from google.oauth2 import service_account

    raw = config.GSC_SERVICE_ACCOUNT_JSON.strip()
    if not raw.startswith("{"):
        raw = base64.b64decode(raw).decode()
    return service_account.Credentials.from_service_account_info(
        json.loads(raw), scopes=["https://www.googleapis.com/auth/webmasters.readonly"])


def gsc_service_email() -> str:
    if not config.GSC_SERVICE_ACCOUNT_JSON:
        return ""
    try:
        return _gsc_creds().service_account_email
    except Exception:
        return "(could not read GSC_SERVICE_ACCOUNT_JSON)"


def gsc_rows(prop: str, days: int = 28) -> list[dict]:
    """[{query, page, clicks, impressions, ctr, position}] for the last `days` days."""
    if not (config.GSC_SERVICE_ACCOUNT_JSON and prop):
        return []
    from googleapiclient.discovery import build

    svc = build("searchconsole", "v1", credentials=_gsc_creds(), cache_discovery=False)
    end = dt.date.today() - dt.timedelta(days=3)
    start = end - dt.timedelta(days=days - 1)
    body = {"startDate": start.isoformat(), "endDate": end.isoformat(),
            "dimensions": ["query", "page"], "rowLimit": 25000}
    rows = svc.searchanalytics().query(siteUrl=prop, body=body).execute().get("rows", [])
    return [{"query": r["keys"][0], "page": r["keys"][1], "clicks": r["clicks"],
             "impressions": r["impressions"], "ctr": r["ctr"], "position": r["position"]} for r in rows]


def domain_of(url: str) -> str:
    return urlparse(url if "://" in url else "https://" + url).netloc.lower().removeprefix("www.")
