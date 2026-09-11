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
import shutil
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


def extract_video_id(url_or_id):
    """Normalize a YouTube URL or standalone 11-char ID to just the 11-char video ID."""
    if not url_or_id:
        return None
    url_or_id = url_or_id.strip()
    match = re.search(r"(?:v=|\/vi\/|\/embed\/|youtu\.be\/|\/v\/|^)([A-Za-z0-9_-]{11})", url_or_id)
    if match:
        return match.group(1)
    return url_or_id


def get_youtube_id_from_sermon_page(sermon_url):
    """Fetch the sermon page and extract the YouTube video ID from og:image thumbnail, iframe, or links."""
    resp = requests.get(sermon_url, headers=REQUEST_HEADERS, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.content, "html.parser")

    # Check og:image meta tag
    og_image = soup.find("meta", property="og:image")
    if og_image and og_image.get("content"):
        match = re.search(r"i\.ytimg\.com/vi/([A-Za-z0-9_-]{11})/", og_image["content"])
        if match:
            return match.group(1)

    # Check iframe embed
    iframes = soup.find_all("iframe")
    for iframe in iframes:
        src = iframe.get("src", "")
        match = re.search(r"youtube\.com/embed/([A-Za-z0-9_-]{11})", src)
        if match:
            return match.group(1)

    # Check any youtube links on page
    links = soup.find_all("a", href=True)
    for link in links:
        href = link["href"]
        match = re.search(r"(?:youtube\.com/watch\?v=|youtu\.be/)([A-Za-z0-9_-]{11})", href)
        if match:
            return match.group(1)

    raise RuntimeError(f"Could not parse YouTube ID from {sermon_url}")


def get_transcript_via_api(video_id, max_retries=3, delay_secs=4):
    """Attempt fetching captions via youtube-transcript-api.
    
    Avoids bot challenges and IP blocking on cloud runners without browser cookies.
    """
    import time
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except ImportError:
        print("  [youtube-transcript-api] Not installed.")
        return None

    for attempt in range(1, max_retries + 1):
        try:
            api = YouTubeTranscriptApi()
            if hasattr(api, "fetch"):
                transcript_obj = api.fetch(video_id)
            elif hasattr(YouTubeTranscriptApi, "get_transcript"):
                transcript_obj = YouTubeTranscriptApi.get_transcript(video_id)
            else:
                return None

            snippets = []
            for s in transcript_obj:
                t = getattr(s, "text", None) or (s.get("text") if isinstance(s, dict) else None)
                if t:
                    snippets.append(t.strip())

            if snippets:
                return " ".join(snippets)
        except Exception as e:
            print(f"  [youtube-transcript-api attempt {attempt}/{max_retries}] {e}")
            if attempt < max_retries:
                time.sleep(delay_secs)

    return None


def get_transcript_via_ytdlp(video_id):
    """Fallback to yt-dlp if installed."""
    video_url = f"https://www.youtube.com/watch?v={video_id}"
    out_template = os.path.join(OUTPUT_DIR, "%(id)s.%(ext)s")

    cmd = [
        "yt-dlp",
        "--skip-download",
        "--write-auto-sub",
        "--write-sub",
        "--sub-lang", "en",
        "--sub-format", "vtt",
        "--remote-components", "ejs:github",
        "-o", out_template,
        video_url,
    ]

    cookies_file = os.environ.get("YOUTUBE_COOKIES_FILE")
    if cookies_file and os.path.exists(cookies_file) and os.path.getsize(cookies_file) > 0:
        cmd += ["--cookies", cookies_file]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"  (yt-dlp exited with {result.returncode})")
            return None

        vtt_path = os.path.join(OUTPUT_DIR, f"{video_id}.en.vtt")
        if not os.path.exists(vtt_path):
            return None

        return vtt_to_plain_text(vtt_path)
    except FileNotFoundError:
        print("  (yt-dlp executable not found in PATH)")
        return None


def get_transcript(video_id):
    """Multi-tier transcript extraction:
    Tier 1: youtube-transcript-api (no cookies, no bot challenges)
    Tier 2: yt-dlp fallback (with cookies if configured)
    """
    print("  Tier 1: Trying youtube-transcript-api...")
    text = get_transcript_via_api(video_id)
    if text:
        print(f"  -> Successfully extracted {len(text)} characters via youtube-transcript-api.")
        return text

    print("  Tier 2: Trying yt-dlp fallback...")
    text = get_transcript_via_ytdlp(video_id)
    if text:
        print(f"  -> Successfully extracted {len(text)} characters via yt-dlp.")
        return text

    raise RuntimeError(
        f"Could not retrieve transcript for video {video_id}.\n"
        f"If the sermon was just streamed, YouTube auto-captions often take 30-90 minutes to process.\n"
        f"You can retry later, or check if captions are available on YouTube."
    )


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


BIBLE_BOOKS = [
    "Genesis", "Exodus", "Leviticus", "Numbers", "Deuteronomy", "Joshua",
    "Judges", "Ruth", "Samuel", "Kings", "Chronicles", "Ezra", "Nehemiah",
    "Esther", "Job", "Psalms?", "Proverbs", "Ecclesiastes",
    "Song of Solomon", "Song of Songs", "Isaiah", "Jeremiah",
    "Lamentations", "Ezekiel", "Daniel", "Hosea", "Joel", "Amos",
    "Obadiah", "Jonah", "Micah", "Nahum", "Habakkuk", "Zephaniah",
    "Haggai", "Zechariah", "Malachi", "Matthew", "Mark", "Luke", "John",
    "Acts", "Romans", "Corinthians", "Galatians", "Ephesians",
    "Philippians", "Colossians", "Thessalonians", "Timothy", "Titus",
    "Philemon", "Hebrews", "James", "Peter", "Jude", "Revelations?",
]

_BOOK_PATTERN = "|".join(sorted(BIBLE_BOOKS, key=len, reverse=True))
_PREFIX_PATTERN = r"(?:[123]|First|Second|Third)\s+"
_PREFIX_NORMALIZE = {"first": "1", "second": "2", "third": "3"}

SCRIPTURE_REF_RE = re.compile(
    rf"\b(?P<prefix>{_PREFIX_PATTERN})?(?P<book>{_BOOK_PATTERN})\s+"
    rf"(?P<chapter>\d{{1,3}})(?::(?P<verse>\d{{1,3}}(?:-\d{{1,3}})?))?\b",
    re.IGNORECASE,
)


def extract_cited_scriptures(transcript, context_chars=120):
    """Best-effort scan of the transcript for scripture references the
    speaker actually said out loud (e.g. "Romans 6:14"), so the plan's
    passage picks can be grounded in what was literally cited rather than
    inferred from vibes alone.

    This runs on auto-generated captions, which regularly mangle spoken
    numbers (e.g. a spoken "Matthew 23:25" was once transcribed here as
    "25. Matthew 23:2." with the digits split across a caption boundary).
    So each match comes back with a chunk of surrounding quoted text,
    letting Claude (or a human) sanity-check/correct the chapter:verse
    against what was actually quoted, instead of trusting the raw digits.

    Returns a list of {"reference": ..., "context": ...} dicts, deduped by
    reference, in order of first mention.
    """
    seen = set()
    results = []
    for m in SCRIPTURE_REF_RE.finditer(transcript):
        prefix = m.group("prefix")
        book = m.group("book")
        chapter = m.group("chapter")
        verse = m.group("verse")

        if prefix:
            prefix_norm = _PREFIX_NORMALIZE.get(prefix.strip().lower(), prefix.strip())
            reference = f"{prefix_norm} {book} {chapter}"
        else:
            reference = f"{book} {chapter}"
        if verse:
            reference += f":{verse}"

        key = reference.lower()
        if key in seen:
            continue
        seen.add(key)

        start = max(0, m.start() - context_chars)
        end = min(len(transcript), m.end() + context_chars)
        results.append({
            "reference": reference,
            "context": transcript[start:end].strip(),
            "position": m.start(),
        })

    return results


DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]


def chunk_transcript(transcript, num_chunks=len(DAYS)):
    """Split the transcript into N sequential, roughly-equal chunks (by
    character count, snapped to the nearest whitespace so words don't get
    cut in half) so each day of the study plan can cover a distinct,
    chronological portion of the sermon — guaranteeing the whole message
    gets walked through by the end of the week instead of just sampling a
    handful of arbitrary moments.

    Returns a list of {"start": int, "end": int, "text": str} dicts.
    """
    n = len(transcript)
    boundaries = [0]
    for i in range(1, num_chunks):
        target = round(n * i / num_chunks)
        window_start = max(0, target - 60)
        window_end = min(n, target + 60)
        window = transcript[window_start:window_end]
        space_offsets = [j for j, ch in enumerate(window) if ch.isspace()]
        if space_offsets:
            best = min(space_offsets, key=lambda j: abs((window_start + j) - target))
            boundaries.append(window_start + best)
        else:
            boundaries.append(target)
    boundaries.append(n)

    chunks = []
    for i in range(num_chunks):
        start, end = boundaries[i], boundaries[i + 1]
        chunks.append({"start": start, "end": end, "text": transcript[start:end].strip()})
    return chunks


def group_citations_by_chunk(cited_scriptures, chunks):
    """Bucket each detected scripture citation into the chunk (day) its
    position in the transcript falls into."""
    grouped = [[] for _ in chunks]
    for citation in cited_scriptures:
        pos = citation["position"]
        for i, chunk in enumerate(chunks):
            if chunk["start"] <= pos < chunk["end"] or i == len(chunks) - 1:
                grouped[i].append(citation)
                break
    return grouped


def generate_with_gemini(prompt, api_key):
    """Call Google AI Studio Gemini API (gemini-2.5-flash) using requests (no extra SDK needed)."""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={api_key}"
    payload = {
        "contents": [
            {
                "parts": [{"text": prompt}]
            }
        ],
        "generationConfig": {
            "temperature": 0.2,
            "responseMimeType": "application/json"
        }
    }
    resp = requests.post(url, json=payload, headers={"Content-Type": "application/json"}, timeout=120)
    resp.raise_for_status()
    data = resp.json()
    try:
        return data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError) as err:
        raise RuntimeError(f"Unexpected response structure from Gemini API: {data}") from err


def generate_with_anthropic(prompt, api_key):
    """Call Anthropic API using official SDK."""
    import anthropic
    client = anthropic.Anthropic(api_key=api_key)
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4000,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(block.text for block in message.content if block.type == "text")


def generate_schedule(memory_verse, sermon_title, transcript, cited_scriptures=None, provider=None):
    """Call Gemini or Anthropic API to turn the verse + transcript into a Mon-Sat study
    plan, returned as structured JSON (one entry per day) so each day's portion
    can be delivered separately later in the week.

    The transcript is split into 6 sequential chunks (one per day) so the
    week walks through the ENTIRE sermon start to finish, rather than
    the LLM sampling a handful of arbitrary moments from the whole thing."""
    gemini_key = os.environ.get("GEMINI_API_KEY")
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")

    if not provider:
        if gemini_key:
            provider = "gemini"
        elif anthropic_key:
            provider = "anthropic"
        else:
            raise RuntimeError(
                "No LLM API key configured. Please set GEMINI_API_KEY (free at https://aistudio.google.com) "
                "or ANTHROPIC_API_KEY in your environment or GitHub Secrets."
            )

    chunks = chunk_transcript(transcript, num_chunks=len(DAYS))
    citations_by_day = group_citations_by_chunk(cited_scriptures or [], chunks)

    day_blocks = []
    for day_name, chunk, day_citations in zip(DAYS, chunks, citations_by_day):
        if day_citations:
            cited_block = "\n".join(
                f'  - {c["reference"]} — nearby quoted text: "...{c["context"]}..."'
                for c in day_citations
            )
        else:
            cited_block = "  (none detected in this segment — infer a passage from its content instead)"

        day_blocks.append(f"""=== {day_name.upper()}'S SEGMENT (roughly {int(chunk['start'] / len(transcript) * 100)}%-{int(chunk['end'] / len(transcript) * 100)}% through the sermon) ===
{chunk['text']}

Scripture references detected in THIS segment (regex-extracted from
auto-generated captions, so chapter/verse DIGITS can be garbled or split
oddly even when the quoted text next to them is accurate — e.g. a spoken
"Matthew 23:25" was once mis-transcribed as "Matthew 23:2" with the "5"
landing on the wrong side of a caption break. Cross-check each reference
against its quoted text and silently correct the chapter/verse if the
quote clearly points to a different one):
{cited_block}""")

    transcript_section = "\n\n".join(day_blocks)

    prompt = f"""You are helping build a personal Bible study schedule for the
six days AFTER a Sunday sermon (Monday through Saturday — Sunday itself is
service day, so don't include it).

MEMORY VERSE FOR THE WEEK:
{memory_verse}

SUNDAY SERMON TITLE: {sermon_title}

The sermon transcript below (auto-generated captions, so some words may be
misheard/imperfect) has been split into 6 SEQUENTIAL, roughly-equal
segments — one per day of the week, in the order they were actually
preached. This is deliberate: by covering each segment on its matching
day, the whole sermon gets walked through start to finish across the
week, instead of only sampling a few arbitrary highlights.

{transcript_section}

For EACH day, using ONLY that day's segment above (not the other days'
segments):

- "focus": a short theme/title for what THAT SEGMENT covered.
- "passages": a LIST of the specific Bible passage(s) that segment
  discussed. Prefer picking from that segment's detected scripture
  references (correcting garbled digits per the instructions above) over
  inventing one — only infer a passage from the segment's general content
  if no reference was detected in it.
- "message_recap": 1-2 sentences factually summarizing what was actually
  said in that segment — stay strictly with what the sermon said, don't
  editorialize through anyone else's teaching style, and don't attribute
  anything to the speaker that they didn't say.
- "message_quote": one short VERBATIM quote (a sentence or two, copied
  exactly, not paraphrased) pulled directly from that day's segment text
  above — something representative of its content.

Each day also needs a "related_scripture" pick: a DIFFERENT passage each
day (don't repeat the same one twice across the week) that meaningfully
connects to the week's MEMORY VERSE itself — a cross-reference, a passage
that uses similar language/imagery, or one that develops the same
theological theme — not just a passage that's thematically close to the
sermon in general. Choose it the way Zac Poonen himself characteristically
would: he draws overwhelmingly on Romans 6-8, Galatians 5, 1 John, James,
and Hebrews 12 to make his points about walking in the Spirit versus the
flesh and practical victory over sin — favor passages from that same
territory over a passage that's merely thematically adjacent (e.g. avoid
reaching for corporate/national-restoration passages like Ezekiel's dry
bones just because it mentions "Spirit" and "life" — that's not the kind
of cross-reference he'd actually make).

For the "verse_reflection" specifically: write it about the memory
verse and that day's related_scripture (not the sermon topic), through
the lens of Zac Poonen's (Christian Fellowship Church, Bangalore)
teaching emphasis — practical, victorious Christian living; real freedom
from the power of sin (not just forgiveness of it); radical self-denial;
walking in the fear of God; and the Holy Spirit's indwelling as the means
of actually being transformed in everyday attitudes and choices, not just
correct doctrine or a warm feeling. Push toward honest, specific
self-examination rather than staying abstract. This is a separate voice
from message_recap/message_quote above and should never be presented as
the sermon speaker's own words.

For "daily_prayer": write 2-3 sentences of sincere, personal prayer applying
the day's scripture and reflection in honest surrender and confession to the
Lord.

Respond with ONLY a JSON array (no other text, no markdown fences), with
exactly 6 objects in this shape, in Monday-through-Saturday order:

[
  {{
    "day": "Monday",
    "focus": "short theme/title for that day's segment",
    "passages": ["specific Bible passage(s) from that day's segment — a list, see instructions above"],
    "message_recap": "1-2 sentences factually summarizing that day's segment",
    "message_quote": "one short verbatim quote copied exactly from that day's segment text",
    "related_scripture": "a passage that ties into the memory verse (see instructions above), different each day",
    "verse_reflection": "one reflection question about the memory verse, in Zac Poonen's teaching voice (see instructions above)",
    "daily_prayer": "2-3 sentences of sincere prayer applying this portion in honest surrender to God"
  }},
  ... (5 more, one per remaining day, in order)
]
"""

    if provider == "gemini":
        if not gemini_key:
            raise RuntimeError("GEMINI_API_KEY environment variable is missing.")
        print("  Calling Google Gemini API (gemini-2.5-flash)...")
        raw_text = generate_with_gemini(prompt, gemini_key)
    elif provider == "anthropic":
        if not anthropic_key:
            raise RuntimeError("ANTHROPIC_API_KEY environment variable is missing.")
        print("  Calling Anthropic API (Claude Sonnet)...")
        raw_text = generate_with_anthropic(prompt, anthropic_key)
    else:
        raise RuntimeError(f"Unknown provider '{provider}'. Choose 'gemini' or 'anthropic'.")

    raw_text = raw_text.strip()
    raw_text = re.sub(r"^```(?:json)?\s*", "", raw_text, flags=re.MULTILINE)
    raw_text = re.sub(r"\s*```$", "", raw_text, flags=re.MULTILINE).strip()

    days = json.loads(raw_text)
    if isinstance(days, dict):
        for key in ("days", "schedule", "plan"):
            if key in days and isinstance(days[key], list):
                days = days[key]
                break

    if not isinstance(days, list) or len(days) != 6:
        raise RuntimeError(f"Expected 6 days in plan, got: {days}")

    return days


def parse_args():
    import argparse
    parser = argparse.ArgumentParser(description="Generate RLCF weekly Bible study plan.")
    parser.add_argument("--video", type=str, help="Specify YouTube video ID or URL directly")
    parser.add_argument("--verse", type=str, help="Specify memory verse text directly")
    parser.add_argument("--provider", type=str, choices=["gemini", "anthropic"], help="LLM provider")
    parser.add_argument("--dry-run", action="store_true", help="Fetch sermon & captions and exit before LLM call")
    return parser.parse_args()


def main():
    args = parse_args()
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    if args.verse:
        print("Using memory verse from argument...")
        memory_verse = args.verse
    elif os.environ.get("MEMORY_VERSE"):
        print("Using memory verse from environment variable...")
        memory_verse = os.environ["MEMORY_VERSE"]
    else:
        print("Fetching this week's memory verse from rlcf.church...")
        memory_verse = get_memory_verse()
    print(f"  -> {memory_verse}")

    sermon_title = "Sunday Sermon"
    sermon_url = "https://rlcf.church/"

    if args.video or os.environ.get("YOUTUBE_VIDEO_ID"):
        raw_vid = args.video or os.environ["YOUTUBE_VIDEO_ID"]
        video_id = extract_video_id(raw_vid)
        sermon_url = f"https://www.youtube.com/watch?v={video_id}"
        print(f"Using provided YouTube video ID: {video_id}")
    else:
        print("Finding latest sermon on rlcf.church...")
        sermon = get_latest_sermon()
        sermon_title = sermon["title"]
        sermon_url = sermon["url"]
        print(f"  -> {sermon_title} ({sermon_url})")

        print("Extracting YouTube video ID...")
        video_id = get_youtube_id_from_sermon_page(sermon_url)
        print(f"  -> {video_id}")

    print("Extracting sermon transcript...")
    transcript = get_transcript(video_id)
    print(f"  -> {len(transcript)} characters of transcript")

    print("Scanning transcript for scripture references the speaker cited...")
    cited_scriptures = extract_cited_scriptures(transcript)
    print(f"  -> {len(cited_scriptures)} candidate reference(s): "
          f"{', '.join(c['reference'] for c in cited_scriptures) or '(none)'}")

    if args.dry_run:
        print("\n=== DRY RUN MODE: Extraction complete! ===")
        print(f"Memory Verse: {memory_verse}")
        print(f"Sermon: {sermon_title} ({video_id})")
        print(f"Transcript length: {len(transcript)} characters")
        print(f"Citations detected: {len(cited_scriptures)}")
        return

    print("Generating Monday-Saturday study schedule...")
    days = generate_schedule(
        memory_verse,
        sermon_title,
        transcript,
        cited_scriptures,
        provider=args.provider
    )

    date_str = datetime.now().strftime("%Y-%m-%d")
    out_path = os.path.join(OUTPUT_DIR, f"week-{date_str}.json")

    payload = {
        "week_of": date_str,
        "memory_verse": memory_verse,
        "sermon_title": sermon_title,
        "sermon_url": sermon_url,
        "days": days,
        "cited_scriptures_detected": cited_scriptures,
    }

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)

    print(f"Saved this week's plan to {out_path}")

    latest_path = os.path.join(OUTPUT_DIR, "latest.json")
    shutil.copyfile(out_path, latest_path)
    print(f"Copied latest plan to {latest_path}")

    print("Delivery happens separately each day via send_daily_portion.py")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
