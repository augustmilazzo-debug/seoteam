---
name: daily-medspa-outreach
description: Find 20 new US med spas, get the email address published on each one's own website, and write each a personalized cold email offering Provahaus's patient-growth service (Gmail drafts by default). Use when asked to run the daily outreach or when the scheduled daily outreach routine fires.
---

# Daily med spa outreach — Provahaus

Today's date is `DATE` (YYYY-MM-DD). Read `config/outreach.yaml`,
`data/outreach/state.yaml`, `data/outreach/prospects.csv` and
`data/outreach/suppression.txt` first.

## Rules
- **Only use email addresses published on the med spa's own website**
  (mailto links, contact page, footer, privacy page). Never guess addresses
  (`info@` included), never use paid lookup tools, never take them from
  third-party directories. No published email → skip that med spa.
- **Never contact a business twice.** Skip any domain or email already in
  `prospects.csv`, in `suppression.txt`, or in `targeting.exclude_domains`.
  Also skip a domain if a Gmail search (`search_threads` for the domain) shows
  earlier contact.
- One email per business, to one address. Prefer a general business address
  over a named employee's personal one.
- Real med spas only (medical aesthetics: injectables, lasers, skin
  treatments). Skip chains/franchises with 10+ locations, directories,
  plastic-surgery hospitals and businesses that look closed.
- **Never invent facts.** Every personal detail in an email must come from
  their site or a search result you actually saw. Never claim Provahaus
  results, clients, prices or guarantees beyond `offer` in the config. Do not
  tell them their ranking or traffic unless you checked it today, and then
  state only what you saw.
- **Do not log in to or submit forms on any site.**
- Email compliance (CAN-SPAM): honest subject line, sender's real name,
  `sender.postal_address`, and an opt-out line in every email.

## 1. Check replies and opt-outs
Search Gmail for replies to earlier outreach (threads with addresses in
`prospects.csv`, last 14 days). For each:
- Unsubscribe / not interested / remove / bounce → add the address (and
  domain for a bounce) to `suppression.txt`; set the row `status` to
  `opted_out` or `bounced`.
- Interested or a question → set `status` to `replied` and list it at the top
  of the summary email. Do not answer it yourself.

## 2. Find 20 new med spas
Start at `state.current_metro_index`. Use WebSearch (queries like
`med spa <metro>`, `medical spa <metro> botox`, `<neighborhood> med spa`) to
collect candidates and their own website domains. When a metro yields no new
candidates after a few searches, add it to `exhausted_metros` and move to the
next one. Keep going until 20 qualified prospects with emails are found or
you have checked 60 candidates; record how many were found if short.

## 3. Research each prospect (own site)
Fetch the home page and the contact/about pages. Record:
- the published email address and the URL it was on;
- business name, city/state, owner or medical director name if shown;
- **one service to hook on**: something they clearly feature or promote
  (a dedicated page, a homepage special, a new device) — this is the service
  Provahaus would help them get more patients for;
- one genuine, specific detail (e.g. a named treatment, their founding story,
  a recent offer). Nothing generic like "great website".

If the site can't be fetched, skip it and note it; do not draft from search
snippets alone.

## 4. Write the email
Plain text, short (90–140 words), written like a person, not a template.
Vary the wording between emails.
- **Subject:** specific and honest, under 50 characters, e.g.
  `More <service> patients for <Business>?`. No ALL CAPS, no "Re:", no emojis.
- **Opening:** address the owner by first name if their site names them,
  else the business name. One sentence with the specific detail you found.
- **Offer:** in 2–3 sentences from `offer.summary`, tied to their hook
  service: they pick the service, we raise their Google visibility for it and
  run ads to a dedicated landing page that brings in new inquiries.
- **Ask:** `offer.call_to_action`. One question, low pressure.
- **Signature:** `sender.name`, `sender.title`, Provahaus, provahaus.com,
  `sender.phone` if set, `sender.postal_address`.
- **Opt-out line:** `If you'd rather not hear from me, just reply "no thanks"
  and I won't email again.`
- Medical-advertising care: no promises of patient numbers or revenue, no
  claims about treatment outcomes.

If any `sender` field is `TODO`, write it as `[ADD YOUR NAME]`,
`[ADD YOUR TITLE]` or `[ADD YOUR MAILING ADDRESS]` in the email so it can't
be missed, and say so at the top of the summary.

## 5. Put it in Gmail
- `daily.mode: drafts` → create one Gmail draft per prospect with
  `create_draft` (to = the prospect's address).
- `daily.mode: send` → send with `send_message` only if every `sender` field
  is filled in; otherwise create drafts and say why in the summary.
Never send more than `daily.prospects` emails in a day.

Append a row per prospect to `prospects.csv` (`status` = `drafted` or `sent`,
with the Gmail draft/message id). Quote CSV fields that contain commas.
Update `state.yaml` (`current_metro_index`, `exhausted_metros`,
`last_run: DATE`).

## 6. Summary email
Send one email to `daily.summary_email_to`, subject
`Provahaus outreach — DATE: <n> drafts ready` (or `<n> sent`):
1. Replies needing you (from step 1), with a link-free one-line summary each.
2. Today's prospects: business, city, email, hook service, subject line.
3. Counts: candidates checked, skipped (no email / duplicate / unreachable),
   drafted or sent, opt-outs added.
4. Anything blocked (sites unreachable, Gmail unavailable, sender details
   missing).

Save the same summary to `reports/outreach/DATE.md`.

## 7. Save state
Commit `data/outreach/` and `reports/outreach/` with the message
`Daily outreach DATE` and push to `main` (`git pull --rebase origin main`
first, then `git push origin HEAD:main`) so tomorrow's run doesn't contact
the same businesses.
