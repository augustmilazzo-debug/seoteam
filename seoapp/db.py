"""Database models. SQLite by default; set DATABASE_URL for Postgres."""

import datetime as dt
import json

from sqlalchemy import (
    JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, create_engine, inspect, text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

from . import config

engine = create_engine(
    config.DATABASE_URL,
    connect_args={"check_same_thread": False, "timeout": 30} if config.DATABASE_URL.startswith("sqlite") else {},
    pool_pre_ping=True,
)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def now() -> dt.datetime:
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Client(Base):
    __tablename__ = "clients"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    url: Mapped[str] = mapped_column(String(500))
    cms: Mapped[str] = mapped_column(String(50), default="unknown")
    notes: Mapped[str] = mapped_column(Text, default="")
    # Business facts extracted from the site (editable): services, locations, etc.
    profile: Mapped[dict] = mapped_column(JSON, default=dict)
    gsc_property: Mapped[str] = mapped_column(String(300), default="")
    # DataForSEO location for rank checks, e.g. "Newport Beach,California,United States"
    rank_location: Mapped[str] = mapped_column(String(200), default="")
    # Google Business Profile facts entered by hand until API access is approved.
    gbp: Mapped[dict] = mapped_column(JSON, default=dict)
    gsc_cache: Mapped[dict] = mapped_column(JSON, default=dict)  # {fetched, rows}
    wp: Mapped[dict] = mapped_column(JSON, default=dict)  # WordPress connection {url, user, app_password}
    compliance_rules: Mapped[str] = mapped_column(Text, default="")  # client-specific advertising rules
    audit_interval_days: Mapped[int] = mapped_column(Integer, default=7)
    rank_interval_days: Mapped[int] = mapped_column(Integer, default=7)
    last_audit_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    last_rank_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    paused: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)

    pages: Mapped[list["Page"]] = relationship(back_populates="client", cascade="all, delete-orphan")
    keywords: Mapped[list["Keyword"]] = relationship(back_populates="client", cascade="all, delete-orphan")
    recommendations: Mapped[list["Recommendation"]] = relationship(back_populates="client", cascade="all, delete-orphan")
    posts: Mapped[list["GbpPost"]] = relationship(back_populates="client", cascade="all, delete-orphan")
    jobs: Mapped[list["Job"]] = relationship(back_populates="client", cascade="all, delete-orphan")
    alerts: Mapped[list["Alert"]] = relationship(back_populates="client", cascade="all, delete-orphan")
    blog_posts: Mapped[list["BlogPost"]] = relationship(back_populates="client", cascade="all, delete-orphan")


class Page(Base):
    """Latest crawled state of one URL."""
    __tablename__ = "pages"
    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"), index=True)
    url: Mapped[str] = mapped_column(String(1000))
    status_code: Mapped[int] = mapped_column(Integer, default=0)
    redirect_to: Mapped[str] = mapped_column(String(1000), default="")
    title: Mapped[str] = mapped_column(Text, default="")
    meta_description: Mapped[str] = mapped_column(Text, default="")
    h1: Mapped[list] = mapped_column(JSON, default=list)
    h2: Mapped[list] = mapped_column(JSON, default=list)
    word_count: Mapped[int] = mapped_column(Integer, default=0)
    canonical: Mapped[str] = mapped_column(String(1000), default="")
    robots_meta: Mapped[str] = mapped_column(String(200), default="")
    indexable: Mapped[bool] = mapped_column(Boolean, default=True)
    schema_types: Mapped[list] = mapped_column(JSON, default=list)
    links_out: Mapped[list] = mapped_column(JSON, default=list)
    inlinks: Mapped[int] = mapped_column(Integer, default=0)
    images_total: Mapped[int] = mapped_column(Integer, default=0)
    images_missing_alt: Mapped[int] = mapped_column(Integer, default=0)
    has_viewport: Mapped[bool] = mapped_column(Boolean, default=True)
    lang: Mapped[str] = mapped_column(String(20), default="")
    og_title: Mapped[bool] = mapped_column(Boolean, default=False)
    response_ms: Mapped[int] = mapped_column(Integer, default=0)
    depth: Mapped[int] = mapped_column(Integer, default=0)
    text: Mapped[str] = mapped_column(Text, default="")  # visible text, truncated
    content_hash: Mapped[str] = mapped_column(String(64), default="")
    changed_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    pagespeed: Mapped[dict] = mapped_column(JSON, default=dict)
    extra: Mapped[dict] = mapped_column(JSON, default=dict)  # phones, address hint, etc.
    last_crawled: Mapped[dt.datetime] = mapped_column(DateTime, default=now)

    client: Mapped[Client] = relationship(back_populates="pages")


class Keyword(Base):
    __tablename__ = "keywords"
    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"), index=True)
    keyword: Mapped[str] = mapped_column(String(300))
    service: Mapped[str] = mapped_column(String(200), default="")
    location: Mapped[str] = mapped_column(String(200), default="")
    intent: Mapped[str] = mapped_column(String(50), default="")
    source: Mapped[str] = mapped_column(String(30), default="ai")  # ai | manual | gsc
    # Tracked keywords are rank-checked and optimized for; others are suggestions.
    tracked: Mapped[bool] = mapped_column(Boolean, default=False)
    priority: Mapped[int] = mapped_column(Integer, default=3)  # 1-5, 5 = most important
    search_volume: Mapped[int | None] = mapped_column(Integer, nullable=True)
    difficulty: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cpc: Mapped[float | None] = mapped_column(Float, nullable=True)
    target_url: Mapped[str] = mapped_column(String(1000), default="")
    role: Mapped[str] = mapped_column(String(20), default="")  # primary | secondary | "" (for target_url's page)
    position: Mapped[float | None] = mapped_column(Float, nullable=True)
    previous_position: Mapped[float | None] = mapped_column(Float, nullable=True)
    best_position: Mapped[float | None] = mapped_column(Float, nullable=True)
    local_pack_position: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ranking_url: Mapped[str] = mapped_column(String(1000), default="")
    gsc_clicks: Mapped[int | None] = mapped_column(Integer, nullable=True)
    gsc_impressions: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Positions per client location for keywords without a city ("botox near me"): {loc_key: {position, maps}}
    loc_ranks: Mapped[dict] = mapped_column(JSON, default=dict)
    competitors: Mapped[list] = mapped_column(JSON, default=list)  # top results from last SERP check
    analysis: Mapped[dict] = mapped_column(JSON, default=dict)  # top-3 competitor page comparison
    history: Mapped[list] = mapped_column(JSON, default=list)  # [{date, position, local, source}]
    last_checked: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)

    client: Mapped[Client] = relationship(back_populates="keywords")


class Recommendation(Base):
    __tablename__ = "recommendations"
    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"), index=True)
    # Stable identity so re-audits update instead of duplicating.
    fingerprint: Mapped[str] = mapped_column(String(300), index=True)
    category: Mapped[str] = mapped_column(String(30))  # technical | on_page | content | new_page | blog | gbp | local
    title: Mapped[str] = mapped_column(String(500))
    details: Mapped[str] = mapped_column(Text, default="")  # markdown
    page_url: Mapped[str] = mapped_column(String(1000), default="")
    keyword: Mapped[str] = mapped_column(String(300), default="")
    impact: Mapped[int] = mapped_column(Integer, default=5)  # 1-10
    effort: Mapped[int] = mapped_column(Integer, default=3)  # 1-10
    confidence: Mapped[float] = mapped_column(Float, default=0.7)  # 0-1
    score: Mapped[float] = mapped_column(Float, default=0)
    level: Mapped[str] = mapped_column(String(20), default="medium")  # critical | high | medium | low
    # open | approved | done | dismissed | resolved (fixed, confirmed by re-audit)
    status: Mapped[str] = mapped_column(String(20), default="open")
    needs_approval: Mapped[bool] = mapped_column(Boolean, default=False)
    location: Mapped[str] = mapped_column(String(100), default="")  # location key for GBP/local items
    draft: Mapped[str] = mapped_column(Text, default="")  # generated content (markdown)
    draft_status: Mapped[str] = mapped_column(String(20), default="")  # "" | draft | approved | rejected
    compliance: Mapped[dict] = mapped_column(JSON, default=dict)  # {status, issues, checked_at, override}
    baseline: Mapped[dict] = mapped_column(JSON, default=dict)  # position etc. when marked done
    result: Mapped[str] = mapped_column(Text, default="")  # measured impact
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)
    last_seen_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)
    done_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)

    client: Mapped[Client] = relationship(back_populates="recommendations")


class GbpPost(Base):
    __tablename__ = "gbp_posts"
    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"), index=True)
    post_type: Mapped[str] = mapped_column(String(30), default="update")  # update | offer | event
    title: Mapped[str] = mapped_column(String(300), default="")
    body: Mapped[str] = mapped_column(Text, default="")
    cta: Mapped[str] = mapped_column(String(50), default="BOOK")
    link: Mapped[str] = mapped_column(String(1000), default="")
    keyword: Mapped[str] = mapped_column(String(300), default="")
    image_idea: Mapped[str] = mapped_column(Text, default="")
    suggested_date: Mapped[dt.date | None] = mapped_column(nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="draft")  # draft | approved | published | rejected
    location: Mapped[str] = mapped_column(String(100), default="")  # location key (see locations.py)
    compliance: Mapped[dict] = mapped_column(JSON, default=dict)  # {status, issues, checked_at, override}
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)

    client: Mapped[Client] = relationship(back_populates="posts")


class BlogPost(Base):
    """A blog post moving from idea -> writing -> review -> approved -> synced (CMS draft) -> published."""
    __tablename__ = "blog_posts"
    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"), index=True)
    source: Mapped[str] = mapped_column(String(20), default="ai")  # ai | manual
    # idea | writing | review | approved | synced | published | rejected
    status: Mapped[str] = mapped_column(String(20), default="idea")
    title: Mapped[str] = mapped_column(String(300))
    target_keyword: Mapped[str] = mapped_column(String(300), default="")
    secondary_keywords: Mapped[list] = mapped_column(JSON, default=list)
    search_intent: Mapped[str] = mapped_column(String(50), default="")
    angle: Mapped[str] = mapped_column(Text, default="")  # idea rationale or the owner's notes
    outline: Mapped[str] = mapped_column(Text, default="")
    supports_url: Mapped[str] = mapped_column(String(1000), default="")  # money page it should link to
    priority: Mapped[int] = mapped_column(Integer, default=3)
    search_volume: Mapped[int | None] = mapped_column(Integer, nullable=True)
    location: Mapped[str] = mapped_column(String(100), default="")  # location key it focuses on, "" = all
    # Written article
    meta_title: Mapped[str] = mapped_column(String(300), default="")
    meta_description: Mapped[str] = mapped_column(String(500), default="")
    slug: Mapped[str] = mapped_column(String(300), default="")
    excerpt: Mapped[str] = mapped_column(Text, default="")
    answer_summary: Mapped[str] = mapped_column(Text, default="")
    key_takeaways: Mapped[list] = mapped_column(JSON, default=list)
    body: Mapped[str] = mapped_column(Text, default="")  # markdown with [[visual:N]] markers
    faqs: Mapped[list] = mapped_column(JSON, default=list)  # [{question, answer}]
    visuals: Mapped[list] = mapped_column(JSON, default=list)
    references: Mapped[list] = mapped_column(JSON, default=list)
    feedback: Mapped[str] = mapped_column(Text, default="")
    # CMS sync
    cms_id: Mapped[str] = mapped_column(String(50), default="")
    cms_edit_url: Mapped[str] = mapped_column(String(1000), default="")
    compliance: Mapped[dict] = mapped_column(JSON, default=dict)  # {status, issues, checked_at, override}
    synced_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)

    client: Mapped[Client] = relationship(back_populates="blog_posts")


class Alert(Base):
    """A notable ranking change, e.g. a keyword moving onto or off page 1."""
    __tablename__ = "alerts"
    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"), index=True)
    kind: Mapped[str] = mapped_column(String(20))  # up | down | new | lost
    keyword: Mapped[str] = mapped_column(String(300))
    page_url: Mapped[str] = mapped_column(String(1000), default="")
    old_position: Mapped[float | None] = mapped_column(Float, nullable=True)
    new_position: Mapped[float | None] = mapped_column(Float, nullable=True)
    message: Mapped[str] = mapped_column(String(500))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)

    client: Mapped[Client] = relationship(back_populates="alerts")


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"), index=True)
    kind: Mapped[str] = mapped_column(String(40))  # audit | ranks | keywords | blog_ideas | gbp_audit | gbp_posts | draft
    status: Mapped[str] = mapped_column(String(20), default="queued")  # queued | running | done | failed
    log: Mapped[str] = mapped_column(Text, default="")
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)
    started_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)

    client: Mapped[Client] = relationship(back_populates="jobs")


class Setting(Base):
    """Agency-wide settings edited in the app (e.g. the compliance checklist sign-off)."""
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON, default=dict)


def get_setting(db, key: str) -> dict:
    row = db.get(Setting, key)
    return dict(row.value or {}) if row else {}


def put_setting(db, key: str, value: dict) -> None:
    row = db.get(Setting, key)
    if row is None:
        db.add(Setting(key=key, value=value))
    else:
        row.value = value
    db.commit()


def init_db() -> None:
    Base.metadata.create_all(engine)
    migrate()


def migrate() -> None:
    """Add columns introduced after a database was created, keeping all data.
    New columns are filled with their model default for existing rows."""
    insp = inspect(engine)
    added: list[str] = []
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            have = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in have:
                    continue
                coltype = col.type.compile(dialect=engine.dialect)
                conn.execute(text(f'ALTER TABLE {table.name} ADD COLUMN "{col.name}" {coltype}'))
                default = col.default.arg if col.default is not None else None
                if callable(default):
                    try:
                        default = default()
                    except TypeError:
                        default = default(None)
                if default is not None:
                    value = json.dumps(default) if isinstance(default, (dict, list)) else default
                    conn.execute(text(f'UPDATE {table.name} SET "{col.name}" = :v'), {"v": value})
                added.append(f"{table.name}.{col.name}")
        if "keywords.role" in added:
            # Rank checks moved from every 3 days to weekly.
            conn.execute(text("UPDATE clients SET rank_interval_days = 7 WHERE rank_interval_days = 3"))
    if added:
        print("Database upgraded, added:", ", ".join(added), flush=True)
