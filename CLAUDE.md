# SEO team for akarimedspa.com

This repo is an AI SEO team for Akari MedSpa (Newport Beach, CA; WordPress).
It audits the site daily, finds the highest-impact opportunities in Google search
and AI assistant citations, and emails the owner ready-to-apply changes.

- Site facts, data sources and settings: `config/site.yaml`
- Agents: `.claude/agents/` · Daily pipeline: `.claude/skills/daily-seo-audit/SKILL.md`
- Memory: `data/recommendations.md` (what was recommended, implemented and measured),
  `data/site-design-system.md`, `data/snapshots/`
- Daily outputs: `reports/YYYY-MM-DD/`

Rules for all agents:
- The owner edits WordPress manually. Never attempt to log in to or modify the site.
- Never invent data. If a source is unavailable, say so.
- This is a medical (YMYL) site: no guaranteed results, no unsupported claims, correct
  product trademarks, credentialed provider named on treatment pages.
- Reuse the site's existing design elements only.
- Balance **enhance** (improve existing pages) with **grow** (new pages, content, GBP,
  reviews, links, AI visibility).
