# Akari MedSpa SEO team

A team of Claude agents that audits https://akarimedspa.com/ every day, finds the
changes with the biggest impact on Google rankings and AI assistant citations,
and emails ready-to-apply instructions.

## Daily pipeline

```
1. AUDIT (parallel)
   search-performance-analyst   Google rankings, clicks, CTR (Search Console + Ahrefs)
   ai-visibility-analyst        Is Akari cited by ChatGPT, Perplexity, Gemini, Claude?
   site-auditor                 Technical/on-page crawl; detects implemented changes
   growth-scout                 New pages, content, GBP, reviews, links, PR
2. opportunity-prioritizer      Today's top 3 by impact × confidence ÷ effort
3. seo-strategist               Exact spec for each change
4. content-designer             Ready-to-paste copy + layout using the site's own elements
5. qa-reviewer                  SEO, design, medical-claims and brand-voice check
6. Daily report emailed; recommendations log updated; results committed
```

Run it manually in Claude Code with: `/daily-seo-audit`

## Setup

1. **Network:** in the cloud environment settings, allow `akarimedspa.com`,
   `googleapis.com`, `api.ahrefs.com` and the AI engine APIs you use
   (`api.anthropic.com`, `api.openai.com`, `api.perplexity.ai`,
   `generativelanguage.googleapis.com`).
2. **Search Console:** create a Google Cloud service account, enable the Search
   Console API, add the service account email as a user on the Search Console
   property, and store its JSON key in the `GSC_SERVICE_ACCOUNT_JSON`
   environment variable (raw or base64).
3. **Ahrefs:** connect the Ahrefs connector in claude.ai, or set `AHREFS_API_KEY`.
4. **AI engines:** set any of `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`,
   `PERPLEXITY_API_KEY`, `GEMINI_API_KEY`. Engines without a key are skipped.
5. **Email:** the Gmail connector sends the daily report to `report.email_to`.
6. **Schedule:** a daily Routine runs `/daily-seo-audit`.

Store secrets in environment settings, never in this repo.

## Files

| Path | Purpose |
|------|---------|
| `config/site.yaml` | Business facts, services, locations, data sources, report settings |
| `config/ai-visibility-prompts.txt` | Questions to test in AI assistants |
| `data/recommendations.md` | Log of every recommendation and its measured impact |
| `data/site-design-system.md` | The site's existing elements, used for every layout |
| `data/snapshots/` | Raw daily data (Search Console, AI visibility, crawl) |
| `reports/YYYY-MM-DD/` | Audits, priorities, specs, implementation packets, daily report |
| `scripts/` | Data collection (`gsc_pull.py`, `ai_visibility.py`) |
