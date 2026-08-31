#!/usr/bin/env python3
"""
RLCF Weekly Bible Study Plan Generator
----------------------------------------
Runs end-to-end, no manual intervention:
  1. Scrapes rlcf.church for this week's memory verse.
  2. Finds the latest sermon page and its YouTube video ID.
  3. Uses yt-dlp to pull the video's caption/transcript.
  4. Sends the verse + transcript to Claude (via the Anthropic API) to
     generate a day-by-day study schedule for the week.
  5. Writes the result to a Markdown file and (optionally) emails it.

Intended to be run by a scheduler (cron / GitHub Actions) every
Sunday night. See README.md for setup instructions.

Requirements:
  pip install requests beautifulsoup4 anthropic yt-dlp

Environment variables required:
  ANTHROPIC_API_KEY   - your Anthropic API key

Optional (for email delivery):
  SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, EMAIL_TO, EMAIL_FROM
"""

import os
import re
import sys
import json
import subprocess
from datetime import datetime

import requests
from bs4 import BeautifulSoup

CHURCH_HOME_URL = "https://rlcf.church/"
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "output")

# Some CDNs/WAFs treat the default python-requests UA + datacenter IPs
# (like GitHub Actions runners) differently than a normal browser request.
# Send a real browser UA on every request to this site to avoid getting
# an unexpected/blocked response.
REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}


def get_memory_verse():
    """Scrape the current 'Adult / Junior' weekly memory verse from rlcf.church."""
    resp = requests.get(CHURCH_HOME_URL, headers=REQUEST_HEADERS, timeout=20)
    resp.raise_for_status()
    print(f"  (fetched {CHURCH_HOME_URL}: status {resp.status_code}, "
          f"{len(resp.content)} bytes)")
    soup = BeautifulSoup(resp.content, "html.parser")

    # Memory verses are rendered as <img alt="..."> with the verse text
    # baked into the alt attribute, followed by a label (Adult / Junior,
    # Primary, Preschool). We want the first "Adult / Junior" one.
    imgs = soup.select("img[alt]")
    candidates = []
    for img in imgs:
        alt = img.get("alt", "")
        if "—" in alt or "-" in alt:  # verse images contain an em-dash before the reference
            candidates.append((img, alt))

    if not candidates:
        raise RuntimeError(
            f"Could not find any memory verse images on rlcf.church "
            f"(fetched {len(imgs)} img[alt] tags total, status {resp.status_code})"
        )

    # The first candidate on the page is the "Adult / Junior" verse.
    verse_text = candidates[0][1].strip()
    return verse_text


def get_latest_sermon():
    """Find the most recent sermon title, URL, speaker, and date from the homepage."""
    resp = requests.get(CHURCH_HOME_URL, headers=REQUEST_HEADERS, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.content, "html.parser")

    sermon_links = soup.select('a[href*="/media/sermons/"]')
    if not sermon_links:
        raise RuntimeError("Could not find any sermon links on rlcf.church")

    latest = sermon_links[0]
    # The page renders titles via a line-clamp widget that duplicates the
    # text in hidden measurement spans, so latest.get_text() comes back
    # garbled/doubled. The clean title lives in a nested h5's title attr.
    title_el = latest.select_one("h5[title]")
    if title_el:
        title = title_el["title"].strip()
    else:
        title = latest.get_text(strip=True)
    href = latest["href"]
    if href.startswith("/"):
        href = "https://rlcf.church" + href

    return {"title": title, "url": href}


def get_youtube_id_from_sermon_page(sermon_url):
    """Fetch the sermon page and extract the YouTube video ID from the og:image thumbnail."""
    resp = requests.get(sermon_url, headers=REQUEST_HEADERS, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.content, "html.parser")

    og_image = soup.find("meta", property="og:image")
    if not og_image:
        raise RuntimeError(f"No og:image meta tag found on {sermon_url}")

    match = re.search(r"i\.ytimg\.com/vi/([A-Za-z0-9_-]{11})/", og_image["content"])
    if not match:
        raise RuntimeError(f"Could not parse YouTube ID from {og_image['content']}")

    return match.group(1)


def get_transcript(video_id):
    """Use yt-dlp to pull the auto-generated (or manual) English transcript for a video."""
    video_url = f"https://www.youtube.com/watch?v={video_id}"
    out_template = os.path.join(OUTPUT_DIR, "%(id)s.%(ext)s")

    cmd = [
        "yt-dlp",
        "--skip-download",
        "--write-auto-sub",
        "--write-sub",
        "--sub-lang", "en",
        "--sub-format", "vtt",
        # Lets yt-dlp download its JS challenge-solver script (runs under
        # Deno, installed by the workflow) to handle YouTube's "n" parameter
        # obfuscation. Without this, format/caption extraction can fail with
        # "The page needs to be reloaded."
        "--remote-components", "ejs:github",
        "-o", out_template,
        video_url,
    ]

    # YouTube blocks caption downloads from datacenter/CI IPs (GitHub Actions
    # included) with "Sign in to confirm you're not a bot" unless yt-dlp
    # authenticates with real browser cookies. If YOUTUBE_COOKIES_FILE points
    # at a cookies.txt (Netscape format), use it.
    cookies_file = os.environ.get("YOUTUBE_COOKIES_FILE")
    if cookies_file and os.path.exists(cookies_file) and os.path.getsize(cookies_file) > 0:
        cmd += ["--cookies", cookies_file]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"yt-dlp failed (exit {result.returncode}) for {video_url}:\n"
            f"--- stdout ---\n{result.stdout}\n"
            f"--- stderr ---\n{result.stderr}"
        )

    vtt_path = os.path.join(OUTPUT_DIR, f"{video_id}.en.vtt")
    if not os.path.exists(vtt_path):
        raise RuntimeError(f"No transcript file found for video {video_id}. "
                            f"The video may not have captions available.")

    return vtt_to_plain_text(vtt_path)


def vtt_to_plain_text(vtt_path):
    """Strip WebVTT timing/formatting down to plain, de-duplicated text."""
    with open(vtt_path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    text_lines = []
    seen = set()
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if line.startswith(("WEBVTT", "Kind:", "Language:")):
            continue
        if re.match(r"^\d{2}:\d{2}:\d{2}", line):  # timestamp line
            continue
        if re.match(r"^\d+$", line):  # cue number
            continue
        clean = re.sub(r"<[^>]+>", "", line)  # strip inline tags
        if clean and clean not in seen:
            text_lines.append(clean)
            seen.add(clean)

    return " ".join(text_lines)


DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]


def generate_schedule(memory_verse, sermon_title, transcript):
    """Call the Anthropic API to turn the verse + transcript into a Mon-Sat study
    plan, returned as structured JSON (one entry per day) so each day's portion
    can be delivered separately later in the week."""
    import anthropic

    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env

    # Cap transcript length to keep the request reasonable
    transcript_excerpt = transcript[:15000]

    prompt = f"""You are helping build a personal Bible study schedule for the
six days AFTER a Sunday sermon (Monday through Saturday — Sunday itself is
service day, so don't include it).

MEMORY VERSE FOR THE WEEK:
{memory_verse}

SUNDAY SERMON TITLE: {sermon_title}

SUNDAY SERMON TRANSCRIPT (may be auto-generated captions, so some words
may be misheard/imperfect):
{transcript_excerpt}

Build a day-by-day study plan for Monday, Tuesday, Wednesday, Thursday,
Friday, and Saturday, based on the memory verse and the actual
content/themes/points of the sermon above. Each day should build toward
having the memory verse fully memorized and the sermon's themes absorbed
by Saturday.

The "focus" and "passage" for each day should be drawn straight from the
actual content/themes/points of the sermon transcript above — keep these
grounded in what was actually preached, not filtered through any
particular teacher's style.

Each day also needs a "related_scripture" pick: a DIFFERENT passage each
day (don't repeat the same one twice across the week) that meaningfully
connects to the week's MEMORY VERSE itself — a cross-reference, a passage
that uses similar language/imagery, or one that develops the same
theological theme — not just a passage that's thematically close to the
sermon in general.

For the "reflection_question" specifically: write it about the memory
verse and that day's related_scripture (not the sermon topic), through
the lens of Zac Poonen's (Christian Fellowship Church, Bangalore)
teaching emphasis — practical, victorious Christian living; real freedom
from the power of sin (not just forgiveness of it); radical self-denial;
walking in the fear of God; and the Holy Spirit's indwelling as the means
of actually being transformed in everyday attitudes and choices, not just
correct doctrine or a warm feeling. Push toward honest, specific
self-examination rather than staying abstract.

Respond with ONLY a JSON array (no other text, no markdown fences), with
exactly 6 objects in this shape:

[
  {{
    "day": "Monday",
    "focus": "short theme/title for the day",
    "passage": "specific Bible passage to read, tied to the sermon's themes",
    "related_scripture": "a passage that ties into the memory verse (see instructions above), different each day",
    "reflection_question": "one reflection question"
  }},
  ... (5 more, one per remaining day)
]
"""

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=3000,
        messages=[{"role": "user", "content": prompt}],
    )

    raw_text = "".join(block.text for block in message.content if block.type == "text")
    raw_text = raw_text.strip()
    # In case the model wraps it in fences despite instructions
    raw_text = re.sub(r"^```(json)?|```$", "", raw_text, flags=re.MULTILINE).strip()

    days = json.loads(raw_text)
    if len(days) != 6:
        raise RuntimeError(f"Expected 6 days back from Claude, got {len(days)}")

    return days


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    print("Fetching this week's memory verse...")
    memory_verse = get_memory_verse()
    print(f"  -> {memory_verse}")

    print("Finding latest sermon...")
    sermon = get_latest_sermon()
    print(f"  -> {sermon['title']} ({sermon['url']})")

    print("Extracting YouTube video ID...")
    video_id = get_youtube_id_from_sermon_page(sermon["url"])
    print(f"  -> {video_id}")

    print("Pulling transcript via yt-dlp...")
    transcript = get_transcript(video_id)
    print(f"  -> {len(transcript)} characters of transcript")

    print("Generating Monday-Saturday study schedule with Claude...")
    days = generate_schedule(memory_verse, sermon["title"], transcript)

    # Saved as JSON, keyed by the Sunday date this week's plan belongs to.
    # send_daily_portion.py reads this file each day (Mon-Sat) and sends
    # just that day's entry.
    date_str = datetime.now().strftime("%Y-%m-%d")
    out_path = os.path.join(OUTPUT_DIR, f"week-{date_str}.json")

    payload = {
        "week_of": date_str,
        "memory_verse": memory_verse,
        "sermon_title": sermon["title"],
        "sermon_url": sermon["url"],
        "days": days,
    }

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)

    print(f"Saved this week's plan to {out_path}")
    print("Delivery happens separately each day via send_daily_portion.py")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
