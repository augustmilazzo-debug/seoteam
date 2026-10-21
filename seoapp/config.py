"""Settings, read from environment variables (set them in the hosting dashboard)."""

import os
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent
DATA_DIR = pathlib.Path(os.environ.get("DATA_DIR", ROOT.parent / "data" / "app"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

DATABASE_URL = os.environ.get("DATABASE_URL") or f"sqlite:///{DATA_DIR / 'seo.db'}"
if DATABASE_URL.startswith("postgres://"):  # Render/Heroku style URL
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-change-me")

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip().strip('"').strip("'")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-5-5")

def _clean(value: str) -> str:
    """Trim spaces, line breaks and wrapping quotes that sneak in when pasting keys."""
    return value.strip().strip('"').strip("'").strip()


DATAFORSEO_LOGIN = _clean(os.environ.get("DATAFORSEO_LOGIN", ""))
DATAFORSEO_PASSWORD = _clean(os.environ.get("DATAFORSEO_PASSWORD", ""))

# One Google service account serves every client: add its email as a user on
# each client's Search Console property.
GSC_SERVICE_ACCOUNT_JSON = os.environ.get("GSC_SERVICE_ACCOUNT_JSON", "")

PAGESPEED_API_KEY = os.environ.get("PAGESPEED_API_KEY", "")

MAX_PAGES = int(os.environ.get("MAX_PAGES", "300"))
USER_AGENT = os.environ.get(
    "CRAWLER_USER_AGENT", "Mozilla/5.0 (compatible; SEOAuditBot/1.0; +site-audit)"
)

# Do not recommend more on-page changes to a page within this many days of a
# change being marked done, so its impact can be measured.
PAGE_COOLDOWN_DAYS = int(os.environ.get("PAGE_COOLDOWN_DAYS", "21"))

# Turn the built-in scheduler off when running tests or one-off scripts.
SCHEDULER_ENABLED = os.environ.get("SCHEDULER_ENABLED", "1") == "1"


def integrations() -> dict:
    return {
        "claude": bool(ANTHROPIC_API_KEY),
        "dataforseo": bool(DATAFORSEO_LOGIN and DATAFORSEO_PASSWORD),
        "gsc": bool(GSC_SERVICE_ACCOUNT_JSON),
        "pagespeed_key": bool(PAGESPEED_API_KEY),
    }
