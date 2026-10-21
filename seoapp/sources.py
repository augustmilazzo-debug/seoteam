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


class DataForSEOAccountError(RuntimeError):
    """Login or billing problem: every further call would fail the same way."""


def _account_problem(code, message: str) -> str | None:
    code = int(code or 0)
    if code in (40100, 40101, 40102, 40103, 40104) or code == 401:
        return ("DataForSEO rejected the login. In Render → Environment, check DATAFORSEO_LOGIN is your API login "
                "(usually your email) and DATAFORSEO_PASSWORD is the API password from dataforseo.com → API Access "
                f"(not your website password). DataForSEO said: {message}")
    if code in (40200, 40201, 40202, 40210) or code == 402:
        return f"DataForSEO account has no funds or the plan doesn't allow this. Add funds at dataforseo.com. DataForSEO said: {message}"
    return None


def _dfs(path: str, payload: list) -> list:
    try:
        r = requests.post(
            f"https://api.dataforseo.com/v3/{path}",
            auth=(config.DATAFORSEO_LOGIN.strip(), config.DATAFORSEO_PASSWORD.strip()),
            json=payload, timeout=120,
        )
    except requests.RequestException as e:
        raise RuntimeError(f"Could not reach DataForSEO: {e}") from e
    try:
        data = r.json()
    except ValueError:
        data = {}
    problem = _account_problem(data.get("status_code") or r.status_code, data.get("status_message") or r.text[:200])
    if problem:
        raise DataForSEOAccountError(problem)
    if r.status_code >= 400 or data.get("status_code") not in (20000, None):
        raise RuntimeError(f"DataForSEO error {data.get('status_code') or r.status_code}: {data.get('status_message') or r.text[:200]}")
    out = []
    for task in data.get("tasks") or []:
        problem = _account_problem(task.get("status_code"), task.get("status_message", ""))
        if problem:
            raise DataForSEOAccountError(problem)
        if task.get("status_code") != 20000:
            raise RuntimeError(f"DataForSEO {path}: {task.get('status_message')} (code {task.get('status_code')})")
        out.append(task.get("result") or [])
    return out


def dfs_test() -> tuple[bool, str]:
    """Free account check: (ok, plain-English message)."""
    login, pw = config.DATAFORSEO_LOGIN.strip(), config.DATAFORSEO_PASSWORD.strip()
    if not login and not pw:
        return False, ("Neither DATAFORSEO_LOGIN nor DATAFORSEO_PASSWORD is set. In Render → your service → "
                       "Environment, add both (names exactly as written, all capitals), then Save.")
    if not login or not pw:
        missing = "DATAFORSEO_LOGIN" if not login else "DATAFORSEO_PASSWORD"
        return False, f"{missing} is missing or empty in Render → Environment. Check the name is spelled exactly like that."
    try:
        r = requests.get("https://api.dataforseo.com/v3/appendix/user_data", auth=(login, pw), timeout=30)
    except requests.RequestException as e:
        return False, f"Could not reach DataForSEO: {e}"
    try:
        data = r.json()
    except ValueError:
        data = {}
    problem = _account_problem(data.get("status_code") or r.status_code, data.get("status_message") or r.text[:200])
    if problem:
        return False, problem
    try:
        info = data["tasks"][0]["result"][0]
    except (KeyError, IndexError, TypeError):
        return False, f"Unexpected answer from DataForSEO (status {r.status_code}): {str(data)[:300]}"
    balance = (info.get("money") or {}).get("balance")
    if balance is not None and balance <= 0:
        return False, f"Logged in as {info.get('login')}, but the balance is ${balance}. Add funds at dataforseo.com."
    return True, f"Connected as {info.get('login')}. Balance: ${balance:,.2f}" if balance is not None else f"Connected as {info.get('login')}."


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
    except DataForSEOAccountError:
        raise
    except RuntimeError:  # usually an unrecognized location name: fall back to the whole US
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
    except DataForSEOAccountError:
        raise
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
# Each client connects with its own Google sign-in (stored refresh token). The older shared
# service account still works for clients that added its email to their property.
GSC_SCOPE = "https://www.googleapis.com/auth/webmasters.readonly"
TOKEN_URI = "https://oauth2.googleapis.com/token"


class GSCError(Exception):
    pass


def _service_account_creds():
    from google.oauth2 import service_account

    raw = config.GSC_SERVICE_ACCOUNT_JSON.strip()
    if not raw.startswith("{"):
        raw = base64.b64decode(raw).decode()
    return service_account.Credentials.from_service_account_info(json.loads(raw), scopes=[GSC_SCOPE])


def gsc_service_email() -> str:
    if not config.GSC_SERVICE_ACCOUNT_JSON:
        return ""
    try:
        return _service_account_creds().service_account_email
    except Exception:
        return "(could not read GSC_SERVICE_ACCOUNT_JSON)"


def google_oauth_ready() -> bool:
    return bool(config.GOOGLE_CLIENT_ID and config.GOOGLE_CLIENT_SECRET)


def google_auth_url(state: str, redirect_uri: str) -> str:
    from urllib.parse import urlencode

    return "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode({
        "client_id": config.GOOGLE_CLIENT_ID, "redirect_uri": redirect_uri, "response_type": "code",
        "scope": f"openid email {GSC_SCOPE}", "access_type": "offline", "prompt": "consent select_account",
        "include_granted_scopes": "true", "state": state})


def google_exchange(code: str, redirect_uri: str) -> dict:
    """Trade the sign-in code for a long-lived refresh token: {refresh_token, email}."""
    r = requests.post(TOKEN_URI, data={"code": code, "client_id": config.GOOGLE_CLIENT_ID,
                                       "client_secret": config.GOOGLE_CLIENT_SECRET,
                                       "redirect_uri": redirect_uri, "grant_type": "authorization_code"}, timeout=30)
    data = r.json() if r.content else {}
    if r.status_code != 200:
        raise GSCError(f"Google sign-in failed: {data.get('error_description') or data.get('error') or r.text[:200]}")
    if GSC_SCOPE not in data.get("scope", ""):
        raise GSCError("Search Console access wasn't granted. Connect again and tick the Search Console permission.")
    if not data.get("refresh_token"):
        raise GSCError("Google didn't return a long-term token. Remove the app's access at "
                       "myaccount.google.com/permissions and connect again.")
    email = ""
    try:  # the id_token comes straight from Google's token endpoint; read it only to show which account
        payload = data["id_token"].split(".")[1]
        email = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))).get("email", "")
    except (KeyError, IndexError, ValueError):
        pass
    return {"refresh_token": data["refresh_token"], "email": email,
            "connected": dt.date.today().isoformat()}


def _client_creds(c):
    auth = c.gsc_auth or {}
    if auth.get("refresh_token") and google_oauth_ready():
        from google.oauth2.credentials import Credentials

        return Credentials(None, refresh_token=auth["refresh_token"], token_uri=TOKEN_URI,
                           client_id=config.GOOGLE_CLIENT_ID, client_secret=config.GOOGLE_CLIENT_SECRET, scopes=[GSC_SCOPE])
    if config.GSC_SERVICE_ACCOUNT_JSON:
        return _service_account_creds()
    return None


def gsc_ready(c) -> bool:
    return bool(c.gsc_property and _client_creds(c) is not None)


def _service(c):
    from googleapiclient.discovery import build

    creds = _client_creds(c)
    if creds is None:
        raise GSCError("Search Console isn't connected for this client.")
    return build("searchconsole", "v1", credentials=creds, cache_discovery=False)


def _explain(e: Exception) -> str:
    msg = str(e)
    if "invalid_grant" in msg:
        return "The Google connection expired or was removed. Click Connect Google Search Console again."
    if "403" in msg or "forbidden" in msg.lower() or "permission" in msg.lower():
        return ("This Google account doesn't have access to that Search Console property. Pick a property from "
                "the list, or ask the owner to add your Google account as a user in Search Console.")
    if "404" in msg:
        return "That Search Console property wasn't found. Pick it from the list instead of typing it."
    return msg[:300]


def gsc_sites(c) -> list[dict]:
    """Properties the connected account can read: [{url, permission}]."""
    try:
        sites = _service(c).sites().list().execute().get("siteEntry", [])
    except GSCError:
        raise
    except Exception as e:
        raise GSCError(_explain(e)) from e
    return sorted(({"url": x["siteUrl"], "permission": x.get("permissionLevel", "")} for x in sites
                   if x.get("permissionLevel") != "siteUnverifiedUser"), key=lambda x: x["url"])


def gsc_rows(c, days: int = 28, offset_days: int = 0, dimensions=("query", "page")) -> list[dict]:
    """[{query, page, clicks, impressions, ctr, position}] for `days` days ending `offset_days` before the latest data."""
    if not gsc_ready(c):
        return []
    end = dt.date.today() - dt.timedelta(days=3 + offset_days)  # Search Console data lags 2-3 days
    start = end - dt.timedelta(days=days - 1)
    body = {"startDate": start.isoformat(), "endDate": end.isoformat(),
            "dimensions": list(dimensions), "rowLimit": 25000}
    try:
        rows = _service(c).searchanalytics().query(siteUrl=c.gsc_property, body=body).execute().get("rows", [])
    except GSCError:
        raise
    except Exception as e:
        raise GSCError(_explain(e)) from e
    out = []
    for r in rows:
        keys = dict(zip(dimensions, r["keys"]))
        out.append({"query": keys.get("query", ""), "page": keys.get("page", ""), "clicks": r["clicks"],
                    "impressions": r["impressions"], "ctr": r["ctr"], "position": r["position"]})
    return out


def gsc_test(c) -> tuple[bool, str]:
    if not c.gsc_property:
        return False, "Choose this client's Search Console property first."
    try:
        rows = gsc_rows(c, days=28)
    except GSCError as e:
        return False, str(e)
    clicks = sum(r["clicks"] for r in rows)
    impressions = sum(r["impressions"] for r in rows)
    return True, (f"Connected to {c.gsc_property}: {len(rows):,} search/page rows, {clicks:,.0f} clicks and "
                  f"{impressions:,.0f} impressions in the last 28 days.")


def domain_of(url: str) -> str:
    return urlparse(url if "://" in url else "https://" + url).netloc.lower().removeprefix("www.")
