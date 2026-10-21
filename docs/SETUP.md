# SEO Command Center: setup guide

This puts the SEO system online at your own private web address. It runs on its own
every day, and you use it from any browser. No coding needed. Plan on about 45 minutes,
most of it creating accounts.

## What you'll sign up for

| Service | Why | Cost (approx.) |
|---|---|---|
| **Render** (render.com) | Hosts the app 24/7 | $7/mo server + $0.25/mo storage |
| **Anthropic** (console.anthropic.com) | Claude: keyword strategy, recommendations, content, GBP posts | ~$5–15 per client per month (more if you request many drafts) |
| **DataForSEO** (dataforseo.com) | Google + Maps rankings, search volumes, competitor pages | ~$1–5 per client per month (pay as you go, $50 minimum deposit) |
| **Google Cloud** | Lets the app read each client's Search Console | Free |

## Step 1: Get your keys (save each one in a notes file as you go)

1. **Claude API key**: go to console.anthropic.com → sign up → *Billing*: add a card and $20
   of credit → *API keys* → **Create key**. Copy it (starts with `sk-ant-`).
2. **DataForSEO**: sign up at dataforseo.com → add funds → *API Access*. Copy the
   **API login** (your email) and **API password** (a separate generated password, not
   your account password).
3. **Google Search Console access** (one time, then it works for every client):
   1. Go to console.cloud.google.com → create a project called "SEO Command Center".
   2. Search for **Google Search Console API** → **Enable**.
   3. Go to *IAM & Admin → Service accounts* → **Create service account** → name it
      `seo-reader` → Done.
   4. Click the new account → *Keys* → **Add key → Create new key → JSON**. A file downloads.
      Open it in a text editor and copy **everything** inside.
   5. Note the account's email (like `seo-reader@your-project.iam.gserviceaccount.com`).
4. **Pick a password** for logging into your dashboard.

## Step 2: Put it online (Render)

1. Sign up at render.com with your GitHub account.
2. Click **New → Blueprint**, choose the `seoteam` repository, and confirm.
   (Until this branch is merged, choose branch `seo-platform`.)
3. Render asks for the secret values. Paste:
   - `APP_PASSWORD`: your dashboard password
   - `ANTHROPIC_API_KEY`: the Claude key
   - `DATAFORSEO_LOGIN` and `DATAFORSEO_PASSWORD`
   - `GSC_SERVICE_ACCOUNT_JSON`: the whole JSON file contents
   - `PAGESPEED_API_KEY`: leave blank (optional)
4. Click **Apply**. After ~3 minutes you get a web address like
   `https://seo-command-center.onrender.com`. Bookmark it and log in.
5. Open **Settings** in the app: every connection should say *Connected*.

## Step 3: Add a client

1. On the Clients page, paste the website, choose the platform and click
   **Add and run first audit**. In about 5–10 minutes the system:
   - crawls every page and checks for technical issues,
   - reads the services and locations,
   - proposes target keywords (the top ones are tracked automatically),
   - checks Google and Maps rankings,
   - writes prioritized recommendations, new page plans and blog ideas.
2. **Connect Search Console** for the client: in Search Console, open their property →
   *Settings → Users and permissions → Add user* → paste the service account email →
   permission *Restricted*. Then in the app: client → **Settings** → enter the property
   (`sc-domain:theirsite.com` for domain properties, or the full `https://` address) → Save.
3. **Check the business profile** (client → Settings). Fix anything the AI got wrong.
   Every recommendation and draft is based on it.
4. **Review keywords** (Keywords tab): tick *Track* on the ones you want, set the stars
   (5 = most important), and save.
5. **Google Business Profile tab**: copy the profile details in from Google and click
   **Save and audit profile**. You'll get GBP recommendations and the first batch of posts
   to approve.

## Your weekly routine (about 20 minutes per client)

1. Open the client → **Overview → Do these next**. They're ranked by score (impact ×
   confidence × ease, boosted for your top keywords). Work top-down.
2. For content items, click the item → **Write the draft** → edit, then **Approve**.
   Nothing is published without your approval.
3. Make the change on the website, then click **Done**. The system saves the current
   ranking, pauses further edits to that page for 3 weeks, and records the result after 14
   days. Technical fixes are confirmed automatically on the next audit ("Verified fixed").
4. **GBP tab**: approve posts, paste them into Google, and click **Published**.
5. Once a month, send the client their **Client report** (print → save as PDF).

## What runs automatically

- **Full re-audit every 7 days** (crawl, technical checks, keyword mapping, page
  optimization, new pages, blog ideas, impact measurement). Google typically recrawls
  important pages of a small site every few days to two weeks, so weekly catches changes
  without wasted work. You can change the interval per client.
- **Rank checks every 3 days** (Google organic + Maps, from the client's city).
- **GBP post drafts** whenever fewer than 3 are waiting.

## Notes

- **Publishing GBP posts automatically** needs Google's Business Profile API approval
  (request it at developers.google.com/my-business, "Request access"; usually 1–4
  weeks). Until then, copy and paste the approved posts.
- **Editing websites automatically** (e.g. pushing approved changes to WordPress) is a
  planned next step. For now, recommendations are ready-to-paste instructions.
- **Cost control**: each weekly audit runs up to 6 AI page reviews per client and skips
  pages that haven't changed since their last review.
- **Backups**: Render → your service → Disks → enable snapshots.

## Blog engine and WordPress

- **Blog tab:** ideas are suggested weekly from the target keywords (spread across locations
  when there are several). Click **Write it**, or add your own topic. Claude writes the full post:
  quick answer, takeaways, question headings, tables/charts, photo spots, FAQs and schema.
- **Connect WordPress** (client → Settings → WordPress connection): in the client's WordPress go to
  **Users → Profile → Application Passwords**, create one named "SEO Command Center", and paste it
  with the username. Click **Save and test connection**.
- After you **Approve** a post, **Send to WordPress as a draft**. It appears in WordPress → Posts →
  Drafts for final review, a featured image, and Publish. Sending again updates the same draft.
  If Yoast or Rank Math doesn't pick up the SEO title/description automatically, copy them from the
  post screen.
- Other platforms (Wix, Squarespace, Webflow): use **Copy HTML** or **Download HTML**.

## Multiple locations

List every location in client → Settings → Business profile (primary first, with nearby areas).
Each location then gets its own Google Business Profile section (details, audit, posts, Maps
rankings), keywords naming a city are checked from that city, "near me" keywords are checked from
every location, and blog ideas include local posts for each city.
