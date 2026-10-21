"""Claude-powered steps: business profiling, keyword strategy, page
optimization, new pages, blog ideas, drafts, and Google Business Profile."""

import json

import anthropic
from pydantic import BaseModel

from . import config

_client = None


def client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY or None)
    return _client


def enabled() -> bool:
    return bool(config.ANTHROPIC_API_KEY)


SYSTEM = """You are a senior local SEO strategist working for an agency. You audit client websites and \
Google Business Profiles and produce changes that move pages up Google's organic results and the Maps / \
local pack.

How you work:
- Be specific and ready to use. When you recommend copy (titles, meta descriptions, headings, paragraphs, \
FAQs, schema), write the exact text, not a description of it.
- Base every recommendation on the data provided. Never invent rankings, search volumes, reviews or facts \
about the business. If information is missing, say what the owner needs to supply, using [brackets].
- Prioritize what actually moves rankings: matching search intent, on-page relevance for the exact \
keyword + city, content depth versus the pages that outrank us, internal links, local signals (NAP \
consistency, LocalBusiness schema, location pages, reviews), and click-through rate.
- Each page should target one primary keyword cluster. Avoid recommending changes that make two pages \
compete for the same keyword.
- Scores: impact 1-10 (effect on rankings/leads), effort 1-10 (1 = minutes, 3 = about an hour, 6 = a day, \
10 = a multi-day project), confidence 0-1.

When the business is medical or aesthetic (medspa, clinic, dental, etc.), Google holds it to its \
"Your Money or Your Life" standard. Then also: never promise or guarantee results; avoid unsupported \
claims ("painless", "permanent", "best", "safest") unless the business can substantiate them; name the \
credentialed provider on treatment pages; mention that results vary and a consultation is needed; use \
correct product trademarks (Botox® Cosmetic, Juvéderm®, Restylane®, Sculptra®, Dysport®, Daxxify®, \
HydraFacial®, Morpheus8®); and do not recommend before/after images without patient consent."""


def _parse(model_cls, prompt: str, max_tokens: int = 16000, effort: str = "medium"):
    resp = client().beta.messages.parse(
        model=config.CLAUDE_MODEL,
        max_tokens=max_tokens,
        system=SYSTEM,
        messages=[{"role": "user", "content": prompt}],
        output_format=model_cls,
        output_config={"effort": effort},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
    if resp.stop_reason == "refusal":
        raise RuntimeError("Claude declined this request")
    if resp.parsed_output is None:
        raise RuntimeError(f"Claude returned no structured output (stop_reason={resp.stop_reason})")
    return resp.parsed_output


def _j(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=1, default=str)


# -- Business profile ---------------------------------------------------------
class Service(BaseModel):
    name: str
    url: str
    description: str


class Location(BaseModel):
    name: str
    address: str
    city: str
    state: str
    phone: str
    dataforseo_location: str  # "City,State Full Name,United States"


class BusinessProfile(BaseModel):
    business_name: str
    business_type: str
    is_medical: bool
    summary: str
    services: list[Service]
    locations: list[Location]
    service_areas: list[str]
    unique_selling_points: list[str]
    providers: list[str]
    brand_voice: str
    booking_url: str


def build_profile(pages: list[dict]) -> dict:
    sample = [{"url": p["url"], "title": p.get("title"), "h1": p.get("h1"), "h2": p.get("h2")[:10],
               "phones": (p.get("extra") or {}).get("phones"), "text": (p.get("text") or "")[:2500]}
              for p in pages[:40]]
    prompt = f"""Here are the main pages of a business website (crawled text, truncated).

Extract the business profile. List every distinct service/treatment the business offers (group close \
variants: e.g. "Botox" and "Dysport" can be separate if they have separate pages, otherwise one \
"Neuromodulators (Botox, Dysport)" entry) with the URL of its page ("" if none). List every physical \
location with its full address and phone as shown on the site. For dataforseo_location use the format \
"City,State Full Name,United States", e.g. "Newport Beach,California,United States". service_areas are \
nearby cities/neighborhoods the business serves or mentions. Use "" or [] when not found; never guess.

PAGES:
{_j(sample)}"""
    return _parse(BusinessProfile, prompt).model_dump()


# -- Keyword strategy ---------------------------------------------------------
class KeywordIdea(BaseModel):
    keyword: str
    service: str
    location: str
    intent: str  # transactional | commercial | informational | navigational
    priority: int  # 1-5
    target_url: str  # best existing page, or "" if a new page is needed
    rationale: str


class KeywordPlan(BaseModel):
    keywords: list[KeywordIdea]


def generate_keywords(profile: dict, pages: list[dict], existing: list[str], gsc_queries: list[dict]) -> list[dict]:
    page_list = [{"url": p["url"], "title": p.get("title"), "h1": (p.get("h1") or [""])[0]} for p in pages[:150]]
    prompt = f"""Build the target keyword strategy for this business.

Create 25-45 keywords people type into Google when they are ready to find or book these services locally. \
Combine each important service with the primary city and the most valuable nearby service areas \
(e.g. "lip filler newport beach", "botox irvine", "medspa near me" style variants only where they fit). \
Include a few high-intent non-geo terms where the page can realistically rank (e.g. "sculptra vs \
radiesse") and the brand-plus-city term. Do not repeat keywords that already exist.

priority 1-5: 5 = core money keyword (high-value service + primary city), 1 = nice to have. Weigh \
service value (a typical treatment ticket), likely demand, and how winnable it is for a local business.

target_url: the ONE existing page that should rank for it (match on page topic), or "" when no page fits \
and a new page should be created. Keep one primary page per keyword cluster.

BUSINESS PROFILE:
{_j(profile)}

SITE PAGES:
{_j(page_list)}

KEYWORDS ALREADY TRACKED (skip these):
{_j(existing)}

REAL GOOGLE SEARCH CONSOLE QUERIES (last 28 days; use these as evidence of demand):
{_j(gsc_queries[:80])}"""
    return [k.model_dump() for k in _parse(KeywordPlan, prompt, effort="high").keywords]


class PageTarget(BaseModel):
    url: str
    rank_worthy: bool  # false for pages not meant to rank (privacy, thank-you, login, tag archives)
    primary_keyword: str
    secondary_keywords: list[str]
    priority: int  # 1-5, business value of ranking this page
    location: str  # city the primary keyword targets, "" if none


class PageTargets(BaseModel):
    pages: list[PageTarget]


def assign_page_keywords(profile: dict, pages: list[dict], gsc_by_page: dict[str, list], taken: list[str]) -> list[dict]:
    """Choose the keywords each page should rank for."""
    view = [{"url": p["url"], "title": p.get("title"), "h1": (p.get("h1") or [""])[0],
             "h2": (p.get("h2") or [])[:8], "words": p.get("word_count"),
             "text": (p.get("text") or "")[:700], "gsc_queries": gsc_by_page.get(p["url"], [])[:10]}
            for p in pages]
    prompt = f"""For EVERY page below, decide the search keywords it should rank for on Google.

- primary_keyword: the single most valuable search this page can realistically win, written the way \
people type it (lowercase). For service pages of a local business, include the city (e.g. \
"lip filler newport beach"). For blog posts, use the question or topic people search. For the \
homepage, the core service + city or brand + city.
- secondary_keywords: 0-2 close variants or related searches the same page should also win.
- Every page must target different keywords: no two pages may share a primary keyword, and avoid \
keywords already used by other pages (listed below).
- If Search Console queries are given, prefer keywords the page already gets impressions for when they \
have real commercial value.
- rank_worthy = false (with empty keywords) for pages that should not compete in search: privacy, \
terms, thank-you, login, cart, author/tag/category archives, image attachment pages, near-empty pages.
- priority 1-5 = business value of this page ranking (5 = core money service page or homepage).

BUSINESS: {_j({k: profile.get(k) for k in ("business_name", "business_type", "locations", "service_areas", "services")})}

KEYWORDS ALREADY USED BY OTHER PAGES: {_j(taken[:300])}

PAGES:
{_j(view)}"""
    return [t.model_dump() for t in _parse(PageTargets, prompt).pages]


class Mapping(BaseModel):
    keyword: str
    target_url: str


class MappingList(BaseModel):
    mappings: list[Mapping]


def map_keywords(keywords: list[str], pages: list[dict]) -> dict[str, str]:
    page_list = [{"url": p["url"], "title": p.get("title"), "h1": (p.get("h1") or [""])[0]} for p in pages[:200]]
    prompt = f"""For each keyword, choose the ONE existing page that should rank for it, or "" if none \
fits and a new page is needed.

KEYWORDS: {_j(keywords)}

PAGES: {_j(page_list)}"""
    return {m.keyword.lower(): m.target_url for m in _parse(MappingList, prompt, effort="low").mappings}


# -- Recommendations ----------------------------------------------------------
class AiRec(BaseModel):
    title: str
    category: str  # on_page | content | new_page | blog | gbp | local | technical
    details: str  # markdown, with the exact copy to use
    keyword: str
    impact: int
    effort: int
    confidence: float


class RecList(BaseModel):
    recommendations: list[AiRec]


class PageReview(BaseModel):
    main_gap: str  # content | intent | local | technical | authority | none
    diagnosis: str  # 2-4 sentences: why the top results outrank this page
    recommendations: list[AiRec]


def optimize_page(profile: dict, page: dict, keywords: list[dict], competitors: list[dict],
                  gsc_queries: list[dict], site_pages: list[str]) -> dict:
    page_view = {k: page.get(k) for k in ("url", "title", "meta_description", "h1", "h2", "word_count",
                                         "schema_types", "inlinks", "images_missing_alt", "pagespeed")}
    page_view["text"] = (page.get("text") or "")[:9000]
    prompt = f"""Optimize this page so it ranks #1 for its target keywords.

First diagnose WHY the top results outrank this page. Compare content depth and topics covered, how \
well each page matches the search intent, local relevance (city, address, map, location schema), \
technical factors (speed, schema, title/H1 match), and the competitors' likely authority (well-known \
directories, national brands and big sites often win on authority, not content; backlink data is not \
available, so say "likely" when inferring it). Set main_gap to the single biggest reason: content, \
intent, local, technical, authority, or none (if the page already ranks top 3).

Then give 3-7 recommendations that close that gap, highest impact first, each a self-contained task \
the owner can do. If the gap is mostly authority, include concrete link and citation building tasks \
(local directories, partner sites, PR, internal links from the strongest pages). Cover, where needed: the title tag and meta description (write them), the H1, missing \
sections/topics competitors cover (write the outline and key copy), an FAQ block (write the questions \
and answers) with FAQPage schema, internal links to add (from which existing page, with what anchor \
text), local relevance (city/neighborhood mentions, embedded map, location schema), trust (provider \
credentials, reviews), and conversion (clear booking CTA). Skip anything the page already does well.

Use category "on_page" for title/meta/heading/schema/link tweaks and "content" for new copy or sections.

BUSINESS: {_j({k: profile.get(k) for k in ("business_name", "business_type", "is_medical", "locations", "service_areas", "unique_selling_points", "providers", "brand_voice", "booking_url")})}

TARGET KEYWORDS FOR THIS PAGE (with current Google position if known):
{_j(keywords)}

PAGE:
{_j(page_view)}

PAGES OUTRANKING US (top results for the main keyword):
{_j(competitors)}

SEARCH CONSOLE QUERIES FOR THIS PAGE (last 28 days):
{_j(gsc_queries[:40])}

OTHER PAGES ON THE SITE (for internal links):
{_j(site_pages[:120])}"""
    return _parse(PageReview, prompt).model_dump()


def new_page_briefs(profile: dict, keywords: list[dict], site_pages: list[str]) -> list[dict]:
    prompt = f"""These target keywords have no page on the site that can rank for them. For each \
group of closely related keywords, recommend one new page (category "new_page"). In details give: the \
URL slug, title tag, meta description, H1, a full section-by-section outline with the key points per \
section (aim for what it takes to outrank local competitors: usually 900-1,500 words), 5 FAQs, schema to \
add, and which existing pages should link to it. If two keywords belong on one page, combine them. For \
location pages for a second office or service area, make the content genuinely unique to that area \
(neighborhoods, directions, parking, local landmarks), never a city-swapped copy.

BUSINESS: {_j(profile)}

KEYWORDS WITHOUT A PAGE: {_j(keywords)}

EXISTING PAGES: {_j(site_pages[:150])}"""
    return [r.model_dump() for r in _parse(RecList, prompt, effort="high").recommendations]


def blog_ideas(profile: dict, keywords: list[dict], blog_titles: list[str], n: int = 6) -> list[dict]:
    prompt = f"""Suggest {n} blog posts (category "blog") that would bring in local searchers who are \
researching treatments, and push link authority to the money pages. Favor real questions people search \
before booking (cost in [city], how long results last, X vs Y, recovery, what to expect, best age to \
start, seasonal topics). In details give: the working title, the target keyword, the search intent, \
an outline (H2/H3), the FAQ questions to answer, which service page it must link to (anchor text), and \
the call to action. Don't duplicate existing posts.

BUSINESS: {_j(profile)}

TARGET KEYWORDS: {_j(keywords)}

EXISTING BLOG POSTS: {_j(blog_titles[:150])}"""
    return [r.model_dump() for r in _parse(RecList, prompt).recommendations]


def gbp_audit(profile: dict, gbp: dict, keywords: list[dict], maps_competitors: list) -> list[dict]:
    prompt = f"""Audit this Google Business Profile for Maps / local pack rankings and give 5-10 \
recommendations (category "gbp"), highest impact first. Cover: primary and secondary categories (name \
the exact Google categories to use), business name compliance (no keyword stuffing), description (write \
a 750-character version), services list with descriptions (write them), products, attributes, hours, \
photos and videos (how many, what to shoot), review volume/velocity/response rate and a review-request \
process, Q&A to seed, posting cadence, booking link with UTM tracking, and NAP consistency with the \
website. Where a field is blank, assume the owner has not filled it in and say what to add.

BUSINESS (from the website): {_j(profile)}

GOOGLE BUSINESS PROFILE (entered by the agency): {_j(gbp)}

TARGET KEYWORDS (with Maps position if known): {_j(keywords)}

TOP MAPS COMPETITORS SEEN: {_j(maps_competitors)}"""
    return [r.model_dump() for r in _parse(RecList, prompt).recommendations]


class Post(BaseModel):
    post_type: str  # update | offer | event
    title: str
    body: str
    cta: str  # BOOK | CALL | LEARN_MORE | ORDER | SIGN_UP
    link: str
    keyword: str
    image_idea: str
    days_from_now: int


class PostList(BaseModel):
    posts: list[Post]


def gbp_posts(profile: dict, keywords: list[dict], recent_titles: list[str], n: int = 8,
              month_hint: str = "") -> list[dict]:
    prompt = f"""Write {n} Google Business Profile posts for the next 4 weeks (about 2 per week; set \
days_from_now to spread them out). Mix: service spotlights tied to a target keyword (use the service + \
city naturally once, no stuffing), seasonal/timely angles {month_hint}, education/FAQ, team/provider \
trust, and at most one offer (only if framed as "[ask owner to confirm offer]"). Each body: 120-250 words, \
plain text (no markdown, no hashtags), friendly and expert, ends with a clear next step. link: the most \
relevant page on the site. image_idea: what photo to attach (no before/after unless consented). Don't \
repeat recent posts.

BUSINESS: {_j(profile)}

TARGET KEYWORDS: {_j(keywords)}

RECENT POSTS: {_j(recent_titles[:30])}"""
    return [p.model_dump() for p in _parse(PostList, prompt).posts]


# -- Drafting -----------------------------------------------------------------
def write_draft(profile: dict, rec: dict, page: dict | None, feedback: str = "") -> str:
    page_part = ""
    if page:
        page_part = f"""
CURRENT PAGE ({page.get('url')}):
Title: {page.get('title')}
Meta: {page.get('meta_description')}
H1: {page.get('h1')}
Text: {(page.get('text') or '')[:9000]}"""
    prompt = f"""Write the finished content for this task, ready to paste into the website.

Output markdown only, starting with a short "Setup" block (URL slug if new, title tag, meta description, \
schema notes, internal links to add), then the full page/post content with H2/H3 headings, then an FAQ \
section. Write in the business's voice, for patients/customers, at an 8th-grade reading level. Use \
[brackets] for any fact you don't have (prices, provider names, years of experience). Make it the most \
helpful page on the topic for someone in this city.

BUSINESS: {_j(profile)}

TASK: {_j(rec)}
{page_part}
{('REVISION FEEDBACK FROM THE AGENCY: ' + feedback) if feedback else ''}"""
    with client().beta.messages.stream(
        model=config.CLAUDE_MODEL,
        max_tokens=64000,
        system=SYSTEM,
        messages=[{"role": "user", "content": prompt}],
        output_config={"effort": "high"},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    ) as stream:
        msg = stream.get_final_message()
    if msg.stop_reason == "refusal":
        raise RuntimeError("Claude declined this request")
    return "".join(b.text for b in msg.content if b.type == "text").strip()
