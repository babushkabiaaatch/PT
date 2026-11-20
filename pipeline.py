#!/usr/bin/env python3
"""
GogginsTube pipeline  (YouTube + Europe PMC research papers + Gemini)
---------------------------------------------------------------------
What this does, in plain words:
  1. Checks both of your keys work (keys live in keys.py)
  2. Searches YouTube twice: once for the all-time best videos, once for the NEWEST ones
  3. Searches Europe PMC (a free database of medical research, including brand-new
     preprints) for recent longevity and mental-toughness papers
  4. Asks Gemini (an AI) to judge everything: keep or reject, plus a topic tag.
     For papers it also writes a one-sentence plain-English summary.
  5. Saves videos.json and papers.json, which your website (index.html) reads

Run it any time you want fresh stuff:   py pipeline.py
You need:                                pip install requests
"""
import os
import re
import json
import time
import datetime
import requests

try:
    from keys import YOUTUBE_KEY, GEMINI_KEY      # on your laptop, keys live in keys.py
except ImportError:                               # in the cloud (GitHub), they come from "secrets"
    YOUTUBE_KEY = os.environ.get("YOUTUBE_API_KEY", "")
    GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")

# ---------------------------------------------------------------
# 1. SETTINGS
# ---------------------------------------------------------------

# If you ever see "model not found" or "no longer available",
# swap in the model name that the message recommends.
GEMINI_MODEL = "gemini-3.5-flash-lite"

VIDEO_SEARCHES = {
    "Goggins": [
        "David Goggins motivation",
        "David Goggins interview",
        "David Goggins podcast",
        "David Goggins speech",
        "David Goggins Can't Hurt Me",
        "David Goggins workout",
    ],
    "Longevity": [
        "longevity science explained",
        "Peter Attia longevity",
        "David Sinclair aging",
        "healthspan how to live longer",
        "Huberman longevity",
        "Bryan Johnson longevity",
        "zone 2 cardio VO2 max longevity",
        "Valter Longo fasting longevity",
        "blue zones longevity",
    ],
}

# A video is only a Longevity CANDIDATE if its title/description has one of these words.
LONGEVITY_WORDS = ["longevity", "aging", "ageing", "healthspan", "lifespan",
                   "live longer", "anti-aging", "vo2 max", "blue zone"]

# Research paper searches (Europe PMC search language: OR, AND, quotes for exact phrases)
PAPER_SEARCHES = {
    "Longevity": 'longevity OR healthspan OR geroscience OR "biological age" OR "lifespan extension" '
                 'OR "epigenetic clock" OR "cellular senescence"',
    "Toughness": '"mental toughness" OR "psychological resilience" OR "ultra-endurance" '
                 'OR "perceived exertion" OR "grit"',
}

PAGES_PER_SEARCH = 2       # all-time pass: each page = up to 50 results
FRESH_DAYS = 45            # "new" videos = published in the last 45 days
NEW_PAPER_DAYS = 60        # papers = first published in the last 60 days
MIN_SECONDS = 240          # skip anything shorter than 4 minutes (kills Shorts)
MAX_FOR_GEMINI = 300       # top-viewed videos sent to Gemini (ALL fresh ones are added on top)
MAX_PAPERS_FOR_GEMINI = 120
VIDEO_BATCH = 40           # videos judged per Gemini request
PAPER_BATCH = 20           # papers judged per Gemini request
PAUSE_BETWEEN_CALLS = 13   # seconds between Gemini calls (free tier = ~5 per minute)

OUT_DIR = "site"           # the website lives in this folder; the pipeline saves its results here

TODAY = datetime.date.today()


# ---------------------------------------------------------------
# 2. KEY CHECKS
# ---------------------------------------------------------------

def check_keys():
    """Test both keys before doing any real work. Returns True if both are OK."""
    keys = [YOUTUBE_KEY, GEMINI_KEY]
    if not all(isinstance(k, str) and k.strip() and "PASTE_" not in k for k in keys):
        print("Open keys.py and paste BOTH keys in it first. (In GitHub: check your two Secrets.)")
        print('Each line should look like:  YOUTUBE_KEY = "AIza...your key..."')
        return False

    ok = True
    r = requests.get("https://www.googleapis.com/youtube/v3/videos",
                     params={"part": "id", "id": "dQw4w9WgXcQ", "key": YOUTUBE_KEY}, timeout=30).json()
    if "error" in r:
        print("X YouTube key FAILED:", r["error"].get("message"))
        print("  -> It must be a key from a Google Cloud project where 'YouTube Data API v3' is enabled.")
        ok = False
    else:
        print("OK YouTube key works")

    r = requests.get(f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}",
                     headers={"x-goog-api-key": GEMINI_KEY}, timeout=30).json()
    if "error" in r:
        print("X Gemini key/model FAILED:", r["error"].get("message"))
        ok = False
    else:
        print("OK Gemini key works")
    return ok


# ---------------------------------------------------------------
# 3. YOUTUBE HELPERS
# ---------------------------------------------------------------

def search_youtube(query, fresh=False):
    """Ask YouTube for videos matching `query`. fresh=True means 'newest first, recent only'."""
    ids = []
    page_token = None
    pages = 1 if fresh else PAGES_PER_SEARCH
    for _ in range(pages):
        params = {"part": "snippet", "q": query, "type": "video",
                  "maxResults": 50, "key": YOUTUBE_KEY}
        if fresh:
            cutoff = TODAY - datetime.timedelta(days=FRESH_DAYS)
            params["order"] = "date"
            params["publishedAfter"] = cutoff.isoformat() + "T00:00:00Z"
        if page_token:
            params["pageToken"] = page_token
        data = requests.get("https://www.googleapis.com/youtube/v3/search",
                            params=params, timeout=30).json()
        if "error" in data:
            print("  YouTube said:", data["error"].get("message"))
            break
        ids.extend(item["id"]["videoId"] for item in data.get("items", []))
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return ids


def get_details(video_ids):
    """Fetch title, channel, length, views for each video (50 at a time)."""
    details = []
    for start in range(0, len(video_ids), 50):
        chunk = video_ids[start:start + 50]
        data = requests.get("https://www.googleapis.com/youtube/v3/videos", params={
            "part": "snippet,contentDetails,statistics",
            "id": ",".join(chunk), "key": YOUTUBE_KEY}, timeout=30).json()
        details.extend(data.get("items", []))
    return details


def duration_to_seconds(iso):
    """YouTube gives lengths like 'PT1H2M3S'. Turn that into seconds."""
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso or "")
    if not m:
        return 0
    hours, minutes, seconds = (int(x or 0) for x in m.groups())
    return hours * 3600 + minutes * 60 + seconds


def guess_category(item, searched_for):
    """Quick keyword check. Returns 'Goggins', 'Longevity', or None (= junk)."""
    title = item["snippet"]["title"].lower()
    channel = item["snippet"]["channelTitle"].lower()
    text = title + " " + item["snippet"].get("description", "").lower()
    if "goggins" in title or "goggins" in channel:
        return "Goggins"
    if searched_for == "Longevity" and any(w in text for w in LONGEVITY_WORDS):
        return "Longevity"
    return None


# ---------------------------------------------------------------
# 4. RESEARCH PAPER HELPERS  (Europe PMC: free, no key needed)
# ---------------------------------------------------------------

def strip_tags(text):
    """Paper titles/abstracts sometimes contain <i>html</i> tags. Remove them."""
    return re.sub(r"<[^>]+>", "", text or "").strip()


def fetch_papers():
    """Download recent papers. Returns a list of paper dicts, newest first."""
    since = (TODAY - datetime.timedelta(days=NEW_PAPER_DAYS)).isoformat()
    papers = {}
    for category, terms in PAPER_SEARCHES.items():
        query = f"({terms}) AND (FIRST_PDATE:[{since} TO {TODAY.isoformat()}]) AND (HAS_ABSTRACT:y)"
        print("Searching papers:", category)
        try:
            data = requests.get("https://www.ebi.ac.uk/europepmc/webservices/rest/search", params={
                "query": query, "format": "json", "resultType": "core", "pageSize": 100,
            }, timeout=60).json()
        except Exception as e:
            print("  Paper search problem:", e)
            continue
        for x in data.get("resultList", {}).get("result", []):
            pid = f"paper:{x.get('source', '')}:{x.get('id', '')}"
            if pid in papers or not x.get("title"):
                continue
            doi = x.get("doi")
            url = f"https://doi.org/{doi}" if doi else f"https://europepmc.org/article/{x.get('source')}/{x.get('id')}"
            authors = [a.strip() for a in (x.get("authorString") or "").rstrip(".").split(",") if a.strip()]
            papers[pid] = {
                "id": pid,
                "title": strip_tags(x["title"]),
                "authors": ", ".join(authors[:3]) + (" et al." if len(authors) > 3 else ""),
                "journal": x.get("journalTitle") or (x.get("bookOrReportDetails") or {}).get("publisher") or "Preprint",
                "date": x.get("firstPublicationDate") or str(x.get("pubYear", "")),
                "url": url,
                "preprint": x.get("source") == "PPR",
                "abstract": strip_tags(x.get("abstractText")),
                "guess": category,
            }
    result = sorted(papers.values(), key=lambda p: p["date"], reverse=True)
    print(f"  Found {len(result)} recent papers.")
    return result


# ---------------------------------------------------------------
# 5. GEMINI HELPERS
# ---------------------------------------------------------------

VIDEO_PROMPT = """You are curating a personal video site with exactly two sections:
- "Goggins": videos featuring or about David Goggins (talks, interviews, podcasts, workouts, motivation).
- "Longevity": videos about living longer and healthier (aging science, healthspan, exercise, sleep or
  nutrition for long life, medical longevity research).
For each numbered video below, decide whether to KEEP it. Reject clickbait, unrelated content,
product ads, and videos that only mention the topic in passing.
Return ONLY a JSON array, one object per video, like this:
[{"i": 0, "keep": true, "cat": "Goggins", "topic": "mental toughness"}]
"cat" must be "Goggins" or "Longevity". "topic" is 2-4 words.

Videos:
"""

PAPER_PROMPT = """You are curating a research section for a personal site about (1) living longer and
healthier (longevity, aging biology, healthspan) and (2) mental toughness and endurance.
For each numbered paper below, decide whether to KEEP it: keep papers that are clearly about those
themes and would interest a curious, non-expert reader. Reject papers that are off-topic or only
mention the theme in passing.
For kept papers also write:
- "summary": ONE plain-English sentence (max 30 words) using ONLY what the abstract says. Never add
  claims, numbers, or advice that are not in the abstract.
- "evidence": one of "human study", "review", "animal study", "cell study", "other".
Return ONLY a JSON array, one object per paper, like this:
[{"i": 0, "keep": true, "cat": "Longevity", "summary": "...", "evidence": "human study"}]
"cat" must be "Longevity" or "Toughness".

Papers:
"""


def call_gemini(prompt):
    """Send a prompt to Gemini and get back a list of JSON objects."""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    body = {"contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0}}
    for attempt in range(3):
        r = requests.post(url, headers={"x-goog-api-key": GEMINI_KEY}, json=body, timeout=120)
        if r.status_code == 429:
            print("  Gemini says slow down. Waiting 40 seconds...")
            time.sleep(40)
            continue
        data = r.json()
        if r.status_code != 200:
            raise RuntimeError(data.get("error", {}).get("message", str(data)))
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        array = re.search(r"\[.*\]", text, re.S).group(0)      # grab the JSON list
        return json.loads(array)
    raise RuntimeError("Gemini kept saying slow down (you may have hit the free daily limit).")


def judge_videos(batch):
    """Returns one label per video in the batch."""
    lines = []
    for i, v in enumerate(batch):
        desc = v["description"][:200].replace("\n", " ")
        lines.append(f"[{i}] {v['title']} | channel: {v['channel']} | {desc}")
    verdicts = {x["i"]: x for x in call_gemini(VIDEO_PROMPT + "\n".join(lines))}
    labels = []
    for i in range(len(batch)):
        x = verdicts.get(i)
        if x and x.get("keep") and x.get("cat") in ("Goggins", "Longevity"):
            labels.append({"keep": True, "cat": x["cat"], "topic": x.get("topic", "")})
        else:
            labels.append({"keep": False})
    return labels


def judge_papers(batch):
    """Returns one label per paper in the batch."""
    lines = []
    for i, p in enumerate(batch):
        lines.append(f"[{i}] {p['title']} | {p['journal']} | {p['abstract'][:700]}")
    verdicts = {x["i"]: x for x in call_gemini(PAPER_PROMPT + "\n".join(lines))}
    labels = []
    for i in range(len(batch)):
        x = verdicts.get(i)
        if x and x.get("keep") and x.get("cat") in ("Longevity", "Toughness") and x.get("summary"):
            labels.append({"keep": True, "cat": x["cat"], "summary": x["summary"],
                           "evidence": x.get("evidence", "other")})
        else:
            labels.append({"keep": False})
    return labels


def load_labels():
    """Gemini's past verdicts are saved in labels.json, so re-running never repeats work."""
    if os.path.exists("labels.json"):
        with open("labels.json") as f:
            return json.load(f)
    return {}


def save_labels(labels):
    with open("labels.json", "w") as f:
        json.dump(labels, f)


def judge_all(items, labels, judge, batch_size):
    """Send items to Gemini in batches. Returns False if Gemini ran into a problem."""
    todo = [x for x in items if x["id"] not in labels]
    print(f"  {len(todo)} need Gemini's verdict ({len(items) - len(todo)} already judged before).")
    for start in range(0, len(todo), batch_size):
        batch = todo[start:start + batch_size]
        try:
            results = judge(batch)
        except Exception as e:
            print("  Gemini problem:", e)
            return False
        for item, label in zip(batch, results):
            labels[item["id"]] = label
        save_labels(labels)                 # saved after every batch, so progress is never lost
        print(f"  Gemini judged {min(start + batch_size, len(todo))} / {len(todo)}")
        time.sleep(PAUSE_BETWEEN_CALLS)
    return True


# ---------------------------------------------------------------
# 6. MAIN PROGRAM
# ---------------------------------------------------------------

def main():
    if not check_keys():
        return

    # --- Videos, step A: search YouTube (all-time best, then newest) ---
    found = {}   # video ID -> which category's search found it
    for category, queries in VIDEO_SEARCHES.items():
        for query in queries:
            print("Searching videos:", query)
            for vid in search_youtube(query):
                found.setdefault(vid, category)
            for vid in search_youtube(query, fresh=True):
                found.setdefault(vid, category)

    # --- Videos, step B: details + cheap keyword filter ---
    print(f"\nFound {len(found)} videos. Fetching details...")
    candidates = []
    for item in get_details(list(found)):
        seconds = duration_to_seconds(item["contentDetails"]["duration"])
        guess = guess_category(item, found[item["id"]])
        if seconds < MIN_SECONDS or guess is None:
            continue
        candidates.append({
            "id": item["id"],
            "title": item["snippet"]["title"],
            "channel": item["snippet"]["channelTitle"],
            "description": item["snippet"].get("description", ""),
            "seconds": seconds,
            "views": int(item["statistics"].get("viewCount", 0)),
            "published": item["snippet"]["publishedAt"][:10],
            "guess": guess,
        })

    # Top-viewed ones, PLUS every fresh one (new videos have few views, so they'd get lost otherwise)
    fresh_cutoff = (TODAY - datetime.timedelta(days=FRESH_DAYS)).isoformat()
    candidates.sort(key=lambda v: -v["views"])
    chosen = candidates[:MAX_FOR_GEMINI]
    chosen_ids = {v["id"] for v in chosen}
    chosen += [v for v in candidates if v["published"] >= fresh_cutoff and v["id"] not in chosen_ids]
    print(f"{len(chosen)} video candidates go to Gemini.")

    # --- Videos, step C: Gemini judges ---
    labels = load_labels()
    gemini_ok = judge_all(chosen, labels, judge_videos, VIDEO_BATCH)
    if not gemini_ok:
        print("  Videos not judged yet will use the keyword guess instead.")

    videos = []
    for v in chosen:
        label = labels.get(v["id"]) or {"keep": True, "cat": v["guess"], "topic": ""}
        if not label["keep"]:
            continue
        videos.append({
            "id": v["id"], "title": v["title"], "channel": v["channel"],
            "seconds": v["seconds"], "views": v["views"], "published": v["published"],
            "category": label["cat"], "topic": label.get("topic", ""),
        })
    videos.sort(key=lambda v: -v["views"])
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "videos.json"), "w") as f:
        json.dump(videos, f)
    goggins = sum(1 for v in videos if v["category"] == "Goggins")
    new_videos = sum(1 for v in videos if v["published"] >= fresh_cutoff)
    print(f"\nSaved {len(videos)} videos ({goggins} Goggins, {len(videos) - goggins} Longevity) to {OUT_DIR}/videos.json")
    print(f"  {new_videos} of them are NEW (published in the last {FRESH_DAYS} days)")

    # --- Papers ---
    print()
    papers = fetch_papers()[:MAX_PAPERS_FOR_GEMINI]
    if papers and gemini_ok:
        gemini_ok = judge_all(papers, labels, judge_papers, PAPER_BATCH)
    elif papers:
        print("Skipping paper judging because Gemini had a problem. Run again later.")

    kept = []
    for p in papers:
        label = labels.get(p["id"])
        if not label or not label["keep"]:       # papers need Gemini's OK (no keyword fallback)
            continue
        kept.append({
            "id": p["id"], "title": p["title"], "authors": p["authors"], "journal": p["journal"],
            "date": p["date"], "url": p["url"], "preprint": p["preprint"],
            "category": label["cat"], "summary": label["summary"], "evidence": label["evidence"],
        })
    with open(os.path.join(OUT_DIR, "papers.json"), "w") as f:
        json.dump(kept, f)
    print(f"Saved {len(kept)} research papers to {OUT_DIR}/papers.json")

    # A tiny status file so the site can say "Updated 3h ago"
    status = {"updated": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
              "videos": len(videos), "new_videos": new_videos, "papers": len(kept)}
    with open(os.path.join(OUT_DIR, "status.json"), "w") as f:
        json.dump(status, f)


if __name__ == "__main__":
    main()
