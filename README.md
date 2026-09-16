# RLCF Bible Study Plan — Automated, Delivered Monday–Saturday

Two automated steps, no manual intervention after setup:

1. **Sunday night:** pulls the current memory verse and latest sermon
   from rlcf.church, grabs that sermon's YouTube transcript, scans it for
   every scripture reference the speaker actually cited, splits the
   transcript into 6 sequential chunks (one per day, in the order they
   were preached), and asks Claude to build a Monday–Saturday study plan
   where each day covers its own chunk — so the whole sermon gets walked
   through start to finish by Saturday, not just a handful of sampled
   moments (Sunday itself is skipped — that's service day).
2. **Every morning, Monday–Saturday:** emails/Slacks you just that day's
   portion — focus, that day's passage(s), a factual recap of that
   segment plus a verbatim quote from it, and a related scripture that
   ties into the week's memory verse with a separate reflection question
   on that (written in Zac Poonen's teaching voice).

## How it works

**`generate_study_plan.py`** (runs once, Sunday night):
1. `get_memory_verse()` scrapes rlcf.church's homepage for the current
   weekly memory verse.
2. `get_latest_sermon()` finds the most recently posted sermon link.
3. `get_youtube_id_from_sermon_page()` reads that sermon page's embedded
   YouTube thumbnail to recover the video ID (the church's site doesn't
   expose a clean video URL directly, but the thumbnail always does).
4. `get_transcript()` transcribes the sermon video directly via the
   Gemini API (`gemini-2.5-flash` using `google-genai`). This bypasses YouTube
   datacenter runner IP blocks, bot challenges, and caption processing delays.
   If `GEMINI_API_KEY` is not set or fails, it falls back to extracting captions
   via `youtube-transcript-api` and `yt-dlp`.
5. `generate_schedule()` sends the verse + transcript to Gemini or Claude and gets
   back a structured 6-day (Mon–Sat) plan as JSON.
6. Saves it to `output/week-YYYY-MM-DD.json` and copies it to `output/latest.json` (served over GitHub Pages via root `index.html`).

**`send_daily_portion.py`** (runs every morning, Mon–Sat):
1. Finds the latest `week-*.json` file.
2. Figures out today's day name.
3. Emails just that day's block from the JSON.
4. On Sunday, it does nothing (by design — Sunday is service day and
   plan-generation day, not a delivery day).

## One-time setup (about 15 minutes)

### 1. Get an API key (Free Gemini or Anthropic)
- **Google Gemini (Recommended & Free):** Get a free API key from [Google AI Studio](https://aistudio.google.com). No credit card required.
  - Set as `GEMINI_API_KEY` in GitHub Secrets.
  - Used for primary cloud-based video transcription (bypassing YouTube runner blocks) and study plan generation.
- **Anthropic Claude (Alternative):** Get a key from [console.anthropic.com](https://console.anthropic.com).
  - Set as `ANTHROPIC_API_KEY` in GitHub Secrets.

### 2. Create a GitHub repo
- Create a new repo (can be private).
- Add all files in this project: `generate_study_plan.py`,
  `send_daily_portion.py`, `requirements.txt`, `index.html`,
  `.github/workflows/weekly-study-plan.yml`,
  `.github/workflows/daily-study-portion.yml`, this `README.md`.
- Ensure `output/` exists for commits.

### 3. Add your secrets
In the repo: **Settings → Secrets and variables → Actions → New repository secret**.

Required for generation (at least one):
- `GEMINI_API_KEY` (free tier via Google AI Studio) OR `ANTHROPIC_API_KEY`

Required for daily delivery (email):
- `SMTP_HOST` (e.g. `smtp.gmail.com`)
- `SMTP_PORT` (e.g. `587`)
- `SMTP_USER` (your email address)
- `SMTP_PASS` (an app password — for Gmail, generate one at
  [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords))
- `EMAIL_TO` (where to send it — can be same as SMTP_USER)
- `EMAIL_FROM` (usually same as SMTP_USER)

Without SMTP secrets set, `send_daily_portion.py` will print that
day's portion to the log instead of emailing it — useful for testing.

Optional, in addition to email:
- `SLACK_WEBHOOK_URL` — an Incoming Webhook URL from a Slack app
  (posts formatted Block Kit messages alongside email).

### 3b. YouTube cookies (needed on GitHub-hosted runners)

YouTube blocks caption requests from cloud IPs (GitHub Actions included)
with "Sign in to confirm you're not a bot". The generator gets past that
with a `YOUTUBE_COOKIES` secret containing a Netscape-format cookies file
for a logged-in YouTube account. Those cookies **expire or get rotated**
every few weeks, and when they do the weekly run fails with
`The provided YouTube account cookies are no longer valid` in the log.

To (re)export them so they last as long as possible (per the
[yt-dlp wiki](https://github.com/yt-dlp/yt-dlp/wiki/Extractors#exporting-youtube-cookies)):

1. Open a **private/incognito** window and sign in to youtube.com
   (a throwaway Google account is safest; YouTube may eventually flag
   the account).
2. In that same window open <https://www.youtube.com/robots.txt>, then
   export cookies with a "Get cookies.txt LOCALLY" style extension.
3. **Close the private window** without signing out. Do not open YouTube
   again in that browser session; using the account in a browser is what
   rotates the cookies and invalidates the exported file.
4. Paste the file's full contents into the `YOUTUBE_COOKIES` repository
   secret, then re-run the **Weekly Bible Study Plan (Generate)** workflow
   from the Actions tab.

### 4. Confirm the schedules
- **Generation** (`weekly-study-plan.yml`) runs automatically at 10:00 PM
  Mountain Time Sunday (04:00 UTC Monday) after the sermon has posted.
  If that run fails (YouTube blocking the runner, captions not processed
  yet), it retries automatically at 1 AM, 5 AM and 10 PM Monday MT; a
  run exits early once the week's plan already exists. You can also run
  it manually from the Actions tab and paste a YouTube video ID/URL to
  bypass sermon detection, or tick **force** to regenerate.
- **Daily send** (`daily-study-portion.yml`) runs 7:00 AM Mountain Time,
  Monday through Saturday.

Both are also runnable on-demand from the repo's **Actions** tab → select the workflow → **Run workflow**.

## Running locally & Previewing

```bash
pip install -r requirements.txt

# Test transcript and scripture extraction without calling LLMs:
python generate_study_plan.py --dry-run

# Preview today's formatted email in your web browser:
python send_daily_portion.py --preview

# Preview a specific day:
python send_daily_portion.py --preview --day Tuesday

# Generate plan using Gemini (free) or Anthropic:
export GEMINI_API_KEY=your-gemini-key
python generate_study_plan.py
```


## Known limitations

- **YouTube runner IP blocks & caption delays:** Because YouTube frequently blocks
  datacenter runner IPs and delays auto-captions, `generate_study_plan.py` uses the
  Gemini API (`gemini-2.5-flash` via `GEMINI_API_KEY`) as its primary video
  transcription method, bypassing YouTube client-side restrictions entirely.
- **Auto-generated captions can misspell names/terms**, so the resulting
  plan may occasionally reflect a transcription error.
- **If rlcf.church changes its page layout**, the scraping functions in
  `generate_study_plan.py` (`get_memory_verse`, `get_latest_sermon`,
  `get_youtube_id_from_sermon_page`) may need small updates to match the
  new HTML structure.
- **If the Sunday generation run fails or is skipped**, the daily sender
  will fall back to the most recent successfully generated week's file
  (it picks the latest `week-*.json` it can find), so you might get a
  repeated/stale plan rather than no email at all — worth checking the
  Sunday job's status if a week's content ever looks off.
