# RLCF Bible Study Plan — Automated, Delivered Monday–Saturday

Two automated steps, no manual intervention after setup:

1. **Sunday night:** pulls the current memory verse and latest sermon
   from rlcf.church, grabs that sermon's YouTube transcript, and asks
   Claude to build a Monday–Saturday study plan (Sunday itself is
   skipped — that's service day).
2. **Every morning, Monday–Saturday:** emails you just that day's
   portion — focus, passage, a related scripture that ties into the
   week's memory verse, and an application prompt.

## How it works

**`generate_study_plan.py`** (runs once, Sunday night):
1. `get_memory_verse()` scrapes rlcf.church's homepage for the current
   weekly memory verse.
2. `get_latest_sermon()` finds the most recently posted sermon link.
3. `get_youtube_id_from_sermon_page()` reads that sermon page's embedded
   YouTube thumbnail to recover the video ID (the church's site doesn't
   expose a clean video URL directly, but the thumbnail always does).
4. `get_transcript()` uses **yt-dlp** to download that video's captions
   (auto-generated or manual) and convert them to plain text.
5. `generate_schedule()` sends the verse + transcript to Claude and gets
   back a structured 6-day (Mon–Sat) plan as JSON.
6. Saves it to `output/week-YYYY-MM-DD.json`.

**`send_daily_portion.py`** (runs every morning, Mon–Sat):
1. Finds the latest `week-*.json` file.
2. Figures out today's day name.
3. Emails just that day's block from the JSON.
4. On Sunday, it does nothing (by design — Sunday is service day and
   plan-generation day, not a delivery day).

## One-time setup (about 15 minutes)

### 1. Get an Anthropic API key
Go to [console.anthropic.com](https://console.anthropic.com), create an
API key. Note: this uses the paid API (separate from a claude.ai
subscription) — cost is a few cents per week, since generation only
happens once.

### 2. Create a GitHub repo
- Create a new repo (can be private).
- Add all the files in this folder to it: `generate_study_plan.py`,
  `send_daily_portion.py`, `requirements.txt`,
  `.github/workflows/weekly-study-plan.yml`,
  `.github/workflows/daily-study-portion.yml`, this `README.md`.
- Create the empty `output/` folder so the Sunday job has somewhere to
  commit into (add a placeholder file like `output/.gitkeep`).

### 3. Add your secrets
In the repo: **Settings → Secrets and variables → Actions → New repository secret**.

Required for generation:
- `ANTHROPIC_API_KEY` — your key from step 1

Required for daily delivery (email):
- `SMTP_HOST` (e.g. `smtp.gmail.com`)
- `SMTP_PORT` (e.g. `587`)
- `SMTP_USER` (your email address)
- `SMTP_PASS` (an app password — for Gmail, generate one at
  [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords))
- `EMAIL_TO` (where to send it — can be same as SMTP_USER)
- `EMAIL_FROM` (usually same as SMTP_USER)

Without SMTP secrets set, `send_daily_portion.py` will just print that
day's portion to the workflow log instead of emailing it — useful for
testing, but you'll want SMTP configured for real hands-off delivery.

### 4. Confirm the schedules
- **Generation** (`weekly-study-plan.yml`) runs 10:00 PM Mountain Time
  Sundays, after the sermon has posted.
- **Daily send** (`daily-study-portion.yml`) runs 7:00 AM Mountain Time,
  Monday through Saturday.

Daylight saving shifts both by an hour part of the year — nudge the cron
hour by 1 if you want them pinned exactly. Both are also runnable
on-demand from the repo's **Actions** tab → select the workflow →
**Run workflow**.

### 5. Test it once manually
- Run **"Weekly Bible Study Plan (Generate)"** manually first, and
  confirm `output/week-<date>.json` gets committed with 6 days' worth of
  content.
- Then run **"Daily Bible Study Portion (Send)"** manually and confirm
  you get an email for whatever day it currently is (it'll do nothing if
  you test it on a Sunday — that's expected).

## Running it locally (optional, for testing)

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=your-key-here
python generate_study_plan.py

# then, any day Mon-Sat:
export SMTP_HOST=smtp.gmail.com SMTP_PORT=587 SMTP_USER=you@gmail.com \
       SMTP_PASS=your-app-password EMAIL_TO=you@gmail.com EMAIL_FROM=you@gmail.com
python send_daily_portion.py
```

## Known limitations

- **If a given Sunday's video has no captions yet** (YouTube sometimes
  takes a few hours to auto-generate them), the Sunday generation run
  will fail. Re-running it later (manually, via Actions) usually
  resolves it.
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
