"""Pull Google Search Console performance data for the site.

Auth: a service account JSON (added as a user on the Search Console property)
stored in the GSC_SERVICE_ACCOUNT_JSON env var, either as raw JSON or base64.

Usage:
    python scripts/gsc_pull.py                # last 28 days vs the previous 28
    python scripts/gsc_pull.py --days 7

Writes data/snapshots/gsc-YYYY-MM-DD.json with page+query rows for both periods.
Search Console data lags ~2-3 days, so the window ends 3 days ago.
"""

import argparse
import base64
import datetime as dt
import json
import os
import pathlib
import sys

from google.oauth2 import service_account
from googleapiclient.discovery import build

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCOPES = ["https://www.googleapis.com/auth/webmasters.readonly"]
DEFAULT_PROPERTY = "sc-domain:akarimedspa.com"


def load_credentials():
    raw = os.environ.get("GSC_SERVICE_ACCOUNT_JSON")
    if not raw:
        sys.exit("GSC_SERVICE_ACCOUNT_JSON is not set; skipping Search Console pull.")
    raw = raw.strip()
    if not raw.startswith("{"):
        raw = base64.b64decode(raw).decode()
    return service_account.Credentials.from_service_account_info(json.loads(raw), scopes=SCOPES)


def query(service, prop, start, end, dimensions, row_limit=25000):
    rows, start_row = [], 0
    while True:
        body = {
            "startDate": start.isoformat(),
            "endDate": end.isoformat(),
            "dimensions": dimensions,
            "rowLimit": row_limit,
            "startRow": start_row,
        }
        resp = service.searchanalytics().query(siteUrl=prop, body=body).execute()
        batch = resp.get("rows", [])
        rows.extend(
            {
                **dict(zip(dimensions, r["keys"])),
                "clicks": r["clicks"],
                "impressions": r["impressions"],
                "ctr": round(r["ctr"], 4),
                "position": round(r["position"], 1),
            }
            for r in batch
        )
        if len(batch) < row_limit:
            return rows
        start_row += row_limit


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=28)
    parser.add_argument("--property", default=os.environ.get("GSC_PROPERTY", DEFAULT_PROPERTY))
    args = parser.parse_args()

    service = build("searchconsole", "v1", credentials=load_credentials(), cache_discovery=False)
    end = dt.date.today() - dt.timedelta(days=3)
    start = end - dt.timedelta(days=args.days - 1)
    prev_end = start - dt.timedelta(days=1)
    prev_start = prev_end - dt.timedelta(days=args.days - 1)

    out = {"property": args.property, "generated": dt.date.today().isoformat(), "periods": {}}
    for label, (s, e) in {"current": (start, end), "previous": (prev_start, prev_end)}.items():
        out["periods"][label] = {
            "start": s.isoformat(),
            "end": e.isoformat(),
            "pages": query(service, args.property, s, e, ["page"]),
            "page_queries": query(service, args.property, s, e, ["page", "query"]),
        }

    path = ROOT / "data" / "snapshots" / f"gsc-{dt.date.today().isoformat()}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, indent=1))
    cur = out["periods"]["current"]
    print(f"Wrote {path.relative_to(ROOT)}: {len(cur['pages'])} pages, {len(cur['page_queries'])} page/query rows")


if __name__ == "__main__":
    main()
