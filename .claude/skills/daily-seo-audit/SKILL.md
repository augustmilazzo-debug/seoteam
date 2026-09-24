---
name: daily-seo-audit
description: Run the full daily SEO pipeline for akarimedspa.com — audit search rankings and AI citations, pick the highest-impact opportunities, spec them, build ready-to-apply implementation packets, QA them, and email the daily report. Use when asked to run the daily audit or when the scheduled daily routine fires.
---

# Daily SEO audit — Akari MedSpa

You are the **SEO Lead**. You coordinate the team in `.claude/agents/`, and
you write the final report. Today's date is `DATE` (YYYY-MM-DD); all outputs
for today go in `reports/DATE/`.

## 0. Setup
- `pip install -q -r requirements.txt`
- Read `config/site.yaml`, `data/recommendations.md`, and the most recent
  report in `reports/` for context.
- Check which data sources are live and note any that are missing:
  `GSC_SERVICE_ACCOUNT_JSON`, Ahrefs (connector or `AHREFS_API_KEY`), AI engine
  keys, and whether `https://akarimedspa.com/` is reachable (`curl -sI`).
  If the site is unreachable, the report must say so prominently.

## 1. Audit (run these four subagents in parallel)
- `search-performance-analyst`: Google rankings, clicks, CTR, trends
- `ai-visibility-analyst`: AI assistant citations and share of voice
- `site-auditor`: technical/on-page crawl and implementation check
- `growth-scout`: new pages, content, GBP, reviews, links, PR

Save each report to `reports/DATE/audit-<agent>.md`.

## 2. Prioritize
Give all four reports to `opportunity-prioritizer`. Save its output to
`reports/DATE/priorities.md`.

## 3. Structure the work
Give today's priorities to `seo-strategist`. Save to `reports/DATE/specs.md`.

## 4. Deliver
Give the specs to `content-designer`, which writes one implementation packet
per priority to `reports/DATE/packet-<n>-<slug>.md`.

## 5. QA
Run `qa-reviewer` on today's packets. Every packet must end at PASS; if it
still fails after one round of fixes, keep it in the report but mark it
"needs your review" with the open issues.

## 6. Update the record
Update `data/recommendations.md`:
- Add each of today's priorities as a new row with status `recommended`.
- Mark items the site-auditor found live as `implemented` with the date.
- For items implemented 14+ days ago, add the measured impact (position,
  clicks, CTR, AI citations before vs now) and set status `measured`.

## 7. Write and send the daily report
Write `reports/DATE/daily-report.md`, then email it (HTML body; keep it
scannable on a phone) to `report.email_to` in `site.yaml` using the Gmail
connector's send tool, subject:
`Akari SEO daily — DATE: <#1 priority in a few words>`.

Report structure:
1. **Today's top actions** (the priorities): for each, what to do, why
   (the evidence in one line), expected impact, time needed, and the packet
   contents. Put the full implementation packet in the email so the owner can
   work from the email alone. Enhance items and grow items are both labeled.
2. **Scoreboard:** Google clicks, impressions, avg position, CTR (28-day, vs
   previous 28); AI share of voice by engine vs last run; number of pages
   indexed; open technical issues.
3. **Wins and losses:** biggest movers since the last report; impact of
   changes implemented earlier.
4. **Still open:** earlier recommendations not yet implemented (one line each).
5. **Watch list:** runners-up and anything worth knowing (new competitor,
   algorithm volatility, seasonality).
6. **Data gaps:** any source that was unavailable today and what it affects.

If the Gmail connector is not available, save the report and say at the top
of your final message that the email could not be sent.

## 8. Save state
Commit `reports/DATE/`, `data/` changes, and new snapshots with the message
`Daily SEO audit DATE`, and push so tomorrow's run can build on today's.
