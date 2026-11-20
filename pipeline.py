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
GEMINI_MODEL = os.environ.get("GEMINI_MODEL") or "gemini-3.5-flash-lite"

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
        "sleep science healthy aging",
        "strength training healthy aging",
        "metabolic health insulin resistance",
        "cardiorespiratory fitness VO2 max science",
        "protein muscle aging research",
        "circadian rhythm sleep health",
        "aging biology geroscience",
    ],
}



# One broad recent-only search per section keeps daily search quota under control.
FRESH_SEARCHES = {
    "Goggins": "David Goggins",
    "Longevity": "longevity healthspan exercise sleep",
}

# Research paper searches (Europe PMC search language: OR, AND, quotes for exact phrases)
PAPER_SEARCHES = {
    "Longevity": 'longevity OR healthspan OR geroscience OR "biological age" OR "lifespan extension" '
                 'OR "epigenetic clock" OR "cellular senescence"',
    "Toughness": '"mental toughness" OR "psychological resilience" OR "ultra-endurance" '
                 'OR "perceived exertion" OR "grit"',
}

PAGES_PER_SEARCH = 1       # one all-time page per query, plus one fresh page; controls YouTube quota
FRESH_DAYS = 45            # "new" videos = published in the last 45 days
NEW_PAPER_DAYS = 60        # papers = first published in the last 60 days
MIN_SECONDS = 240          # skip anything shorter than 4 minutes (kills Shorts)
MAX_FOR_GEMINI = 300       # total all-time candidates sent to Gemini across both sections
MAX_PER_CATEGORY = MAX_FOR_GEMINI // 2  # reserve half the review capacity for each section
MAX_PAPERS_FOR_GEMINI = 120
VIDEO_BATCH = 40           # videos judged per Gemini request
PAPER_BATCH = 20           # papers judged per Gemini request
PAUSE_BETWEEN_CALLS = 13   # seconds between Gemini calls (free tier = ~5 per minute)

OUT_DIR = "site"           # the website lives in this folder; the pipeline saves its results here

TODAY = datetime.date.today()

# Tracks whether the YouTube API has exhausted its daily quota.
YOUTUBE_QUOTA_EXHAUSTED = False


# ---------------------------------------------------------------
# 2. KEY CHECKS
# ---------------------------------------------------------------

def check_keys():
    """Validate the configured API keys before starting discovery."""
    if not YOUTUBE_KEY or not GEMINI_KEY:
        print("Missing API keys.")
        print("GitHub Actions needs repository secrets named YOUTUBE_API_KEY and GEMINI_API_KEY.")
        print("For local runs, provide keys.py with YOUTUBE_KEY and GEMINI_KEY.")
        return False

    try:
        youtube_response = requests.get(
            "https://www.googleapis.com/youtube/v3/videos",
            params={
                "part": "id",
                "id": "dQw4w9WgXcQ",
                "key": YOUTUBE_KEY,
            },
            timeout=30,
        )
        youtube_data = youtube_response.json()
        if youtube_response.status_code != 200 or "error" in youtube_data:
            message = youtube_data.get("error", {}).get("message", youtube_response.text[:500])
            print(f"YouTube API key/check failed (HTTP {youtube_response.status_code}): {message}")
            return False
        print("OK YouTube API key works")
    except Exception as exc:
        print(f"YouTube API key check could not complete: {exc}")
        return False

    try:
        gemini_response = requests.get(
            f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}",
            headers={"x-goog-api-key": GEMINI_KEY},
            timeout=30,
        )
        gemini_data = gemini_response.json()
        if gemini_response.status_code != 200 or "error" in gemini_data:
            message = gemini_data.get("error", {}).get("message", gemini_response.text[:500])
            print(f"Gemini model/key check failed (HTTP {gemini_response.status_code}): {message}")
            print(f"Configured model: {GEMINI_MODEL}")
            return False
        print(f"OK Gemini API key and model work ({GEMINI_MODEL})")
    except Exception as exc:
        print(f"Gemini API key check could not complete: {exc}")
        return False

    return True


# ---------------------------------------------------------------
# 3. YOUTUBE HELPERS
# ---------------------------------------------------------------


def search_youtube(query, fresh=False):
    """Search YouTube, handling exhausted API quota gracefully."""

    global YOUTUBE_QUOTA_EXHAUSTED

    if YOUTUBE_QUOTA_EXHAUSTED:
        print(f"Skipping YouTube search: quota exhausted ({query})")
        return []

    ids = []
    page_token = None
    pages = 1 if fresh else PAGES_PER_SEARCH

    for _ in range(pages):
        params = {
            "part": "snippet",
            "q": query,
            "type": "video",
            "maxResults": 50,
            "key": YOUTUBE_KEY,
        }

        if fresh:
            cutoff = TODAY - datetime.timedelta(days=FRESH_DAYS)
            params["order"] = "date"
            params["publishedAfter"] = cutoff.isoformat() + "T00:00:00Z"

        if page_token:
            params["pageToken"] = page_token

        response = requests.get(
            "https://www.googleapis.com/youtube/v3/search",
            params=params,
            timeout=45,
        )

        try:
            data = response.json()
        except ValueError:
            raise RuntimeError(
                f"YouTube returned invalid JSON for {query} "
                f"(HTTP {response.status_code})"
            )

        if response.status_code != 200 or "error" in data:
            error = data.get("error", {})
            message = error.get("message", response.text[:500])

            reasons = [
                item.get("reason", "")
                for item in error.get("errors", [])
            ]

            quota_errors = {
                "quotaExceeded",
                "dailyLimitExceeded",
                "rateLimitExceeded",
            }

            if (
                response.status_code == 429
                or quota_errors.intersection(reasons)
            ):
                YOUTUBE_QUOTA_EXHAUSTED = True
                print(
                    "WARNING: YouTube API quota exhausted. "
                    "Skipping remaining searches."
                )
                return ids

            raise RuntimeError(
                f"YouTube search failed for '{query}' "
                f"(HTTP {response.status_code}): {message}"
            )

        ids.extend(
            item["id"]["videoId"]
            for item in data.get("items", [])
            if item.get("id", {}).get("videoId")
        )

        page_token = data.get("nextPageToken")

        if not page_token:
            break

    return ids



def get_details(video_ids):
    """Fetch title, channel, length and views for each video (50 at a time)."""
    details = []
    for start in range(0, len(video_ids), 50):
        chunk = video_ids[start:start + 50]
        response = requests.get(
            "https://www.googleapis.com/youtube/v3/videos",
            params={
                "part": "snippet,contentDetails,statistics",
                "id": ",".join(chunk),
                "key": YOUTUBE_KEY,
            },
            timeout=45,
        )
        try:
            data = response.json()
        except ValueError:
            raise RuntimeError(
                f"YouTube details returned non-JSON (HTTP {response.status_code})."
            )
        if response.status_code != 200 or "error" in data:
            message = data.get("error", {}).get("message", response.text[:500])
            raise RuntimeError(
                f"YouTube details failed (HTTP {response.status_code}): {message}"
            )
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
    """Assign a broad candidate category from all searches that found this video."""
    snippet = item.get("snippet", {})
    title = snippet.get("title", "").lower()
    channel = snippet.get("channelTitle", "").lower()
    searched_categories = set(searched_for or [])

    # Explicit Goggins matches take priority when a video appears in both streams.
    if "goggins" in title or "goggins" in channel:
        return "Goggins"

    # A video discovered by any longevity query is eligible for Gemini review.
    if "Longevity" in searched_categories:
        return "Longevity"

    return None


# ---------------------------------------------------------------
# 4. RESEARCH PAPER HELPERS  (Europe PMC: free, no key needed)
# ---------------------------------------------------------------

def strip_tags(text):
    """Paper titles/abstracts sometimes contain <i>html</i> tags. Remove them."""
    return re.sub(r"<[^>]+>", "", text or "").strip()


def fetch_papers():
    """Download recent papers. Raises only when every search request fails."""
    since = (TODAY - datetime.timedelta(days=NEW_PAPER_DAYS)).isoformat()
    papers = {}
    successful_searches = 0
    failures = []

    for category, terms in PAPER_SEARCHES.items():
        query = (
            f"({terms}) AND (FIRST_PDATE:[{since} TO {TODAY.isoformat()}]) "
            "AND (HAS_ABSTRACT:y)"
        )
        print("Searching papers:", category)
        try:
            response = requests.get(
                "https://www.ebi.ac.uk/europepmc/webservices/rest/search",
                params={
                    "query": query,
                    "format": "json",
                    "resultType": "core",
                    "pageSize": 100,
                },
                timeout=60,
            )
            response.raise_for_status()
            data = response.json()
            if data.get("error"):
                raise RuntimeError(str(data["error"]))
            successful_searches += 1
        except Exception as exc:
            failures.append(f"{category}: {exc}")
            print(f"  Paper search problem ({category}): {exc}")
            continue

        for item in data.get("resultList", {}).get("result", []):
            paper_id = f"paper:{item.get('source', '')}:{item.get('id', '')}"
            if paper_id in papers or not item.get("title"):
                continue

            doi = item.get("doi")
            url = (
                f"https://doi.org/{doi}"
                if doi
                else f"https://europepmc.org/article/{item.get('source')}/{item.get('id')}"
            )
            authors = [
                author.strip()
                for author in (item.get("authorString") or "").rstrip(".").split(",")
                if author.strip()
            ]
            papers[paper_id] = {
                "id": paper_id,
                "title": strip_tags(item["title"]),
                "authors": ", ".join(authors[:3]) + (" et al." if len(authors) > 3 else ""),
                "journal": item.get("journalTitle")
                    or (item.get("bookOrReportDetails") or {}).get("publisher")
                    or "Preprint",
                "date": item.get("firstPublicationDate") or str(item.get("pubYear", "")),
                "url": url,
                "preprint": item.get("source") == "PPR",
                "abstract": strip_tags(item.get("abstractText")),
                "guess": category,
            }

    if successful_searches == 0:
        raise RuntimeError(
            "All Europe PMC searches failed. Existing papers.json will be preserved. "
            + " | ".join(failures)
        )

    result = sorted(papers.values(), key=lambda paper: paper["date"], reverse=True)
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
product ads, and videos that only mention the topic in passing. Each video includes a candidate
section based on the YouTube search that found it. Use that as a strong category hint, while still
checking that the title and description genuinely fit the site's topic.
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
    """Send a prompt to Gemini and safely parse its JSON-array response."""
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent"
    )
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": 0,
        },
    }

    for attempt in range(3):
        response = requests.post(
            url,
            headers={"x-goog-api-key": GEMINI_KEY},
            json=body,
            timeout=120,
        )
        try:
            data = response.json()
        except ValueError:
            raise RuntimeError(
                f"Gemini returned non-JSON (HTTP {response.status_code}): "
                f"{response.text[:500]}"
            )

        if response.status_code == 429:
            wait_seconds = 40 * (attempt + 1)
            print(f"  Gemini rate limit reached. Waiting {wait_seconds} seconds...")
            time.sleep(wait_seconds)
            continue

        if response.status_code != 200:
            message = data.get("error", {}).get("message", str(data))
            raise RuntimeError(f"Gemini API error (HTTP {response.status_code}): {message}")

        candidates = data.get("candidates") or []
        if not candidates:
            raise RuntimeError(
                "Gemini returned no candidates. Response: " + json.dumps(data)[:1000]
            )

        parts = candidates[0].get("content", {}).get("parts", [])
        response_text = "".join(part.get("text", "") for part in parts)
        if not response_text.strip():
            raise RuntimeError("Gemini returned an empty response.")

        try:
            parsed = json.loads(response_text)
        except json.JSONDecodeError:
            match = re.search(r"\[.*\]", response_text, re.S)
            if not match:
                raise RuntimeError(
                    "Gemini response did not contain a JSON array: "
                    + response_text[:1000]
                )
            parsed = json.loads(match.group(0))

        if not isinstance(parsed, list):
            raise RuntimeError("Gemini response was valid JSON but not a list.")
        return parsed

    raise RuntimeError(
        "Gemini kept returning HTTP 429 after retries. "
        "Check the API quota or try the workflow again later."
    )


def judge_videos(batch):
    """Returns one label per video in the batch."""
    lines = []
    for i, v in enumerate(batch):
        desc = v["description"][:200].replace("\n", " ")
        lines.append(
            f"[{i}] candidate section: {v['guess']} | {v['title']} "
            f"| channel: {v['channel']} | {desc}"
        )
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
    write_json_atomic("labels.json", labels)


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


def load_existing_list(path):
    """Load an existing website JSON list; never silently discard a corrupt file."""
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as file:
        data = json.load(file)
    if not isinstance(data, list):
        raise RuntimeError(f"{path} must contain a JSON list. Existing file was left untouched.")
    return data


def write_json_atomic(path, data):
    """Write JSON via a temporary file so an interrupted run cannot truncate the live file."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    temporary_path = path + ".tmp"
    with open(temporary_path, "w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)
        file.write("\n")
    os.replace(temporary_path, path)


def merge_by_id(existing, discovered):
    """Merge newly discovered items into the saved library without duplicates or data loss."""
    merged = {}
    for item in existing:
        item_id = item.get("id")
        if item_id:
            merged[item_id] = item
    for item in discovered:
        item_id = item.get("id")
        if item_id:
            merged[item_id] = {**merged.get(item_id, {}), **item}
    return list(merged.values())


# ---------------------------------------------------------------
# 6. MAIN PROGRAM
# ---------------------------------------------------------------

def main():
    if not check_keys():
        raise SystemExit("API key validation failed. No website data files were changed.")

    os.makedirs(OUT_DIR, exist_ok=True)
    videos_path = os.path.join(OUT_DIR, "videos.json")
    papers_path = os.path.join(OUT_DIR, "papers.json")
    status_path = os.path.join(OUT_DIR, "status.json")

    existing_videos = load_existing_list(videos_path)
    existing_papers = load_existing_list(papers_path)

    # --- Videos, step A: search YouTube (all-time results, then recent results) ---
    # Store every category that found an ID. A first-match-only dict loses overlaps.
    found = {}  # video ID -> set of source categories
    search_count = 0
    for category, queries in VIDEO_SEARCHES.items():
        for query in queries:
            print("Searching videos:", query)
            for video_id in search_youtube(query):
                found.setdefault(video_id, set()).add(category)
            search_count += 1

        fresh_query = FRESH_SEARCHES.get(category)
        if fresh_query:
            print("Searching recent videos:", fresh_query)
            for video_id in search_youtube(fresh_query, fresh=True):
                found.setdefault(video_id, set()).add(category)
            search_count += 1

    if not found:
        raise RuntimeError(
            "YouTube searches returned zero video IDs. This usually indicates an API, "
            "quota, network or search configuration problem. Existing website data "
            "has been preserved."
        )

    print(f"\\nFound {len(found)} unique video IDs across {search_count} searches. Fetching details...")
    details = get_details(list(found))
    if not details:
        raise RuntimeError(
            "YouTube returned no video details for the discovered IDs. "
            "Existing website data has been preserved."
        )

    # --- Videos, step B: remove Shorts and assign broad candidate categories ---
    candidates = []
    for item in details:
        snippet = item.get("snippet") or {}
        content_details = item.get("contentDetails") or {}
        if not snippet or not content_details.get("duration"):
            continue

        seconds = duration_to_seconds(content_details["duration"])
        guess = guess_category(item, found.get(item.get("id"), set()))
        if seconds < MIN_SECONDS or guess is None:
            continue

        candidates.append({
            "id": item["id"],
            "title": snippet.get("title", ""),
            "channel": snippet.get("channelTitle", ""),
            "description": snippet.get("description", ""),
            "seconds": seconds,
            "views": int((item.get("statistics") or {}).get("viewCount", 0)),
            "published": snippet.get("publishedAt", "")[:10],
            "guess": guess,
        })

    print(
        f"Eligible candidates after duration/category checks: {len(candidates)} "
        f"(minimum duration: {MIN_SECONDS // 60} minutes)."
    )

    fresh_cutoff = (TODAY - datetime.timedelta(days=FRESH_DAYS)).isoformat()

    # Select candidates separately so a large/high-view category cannot crowd out the other.
    chosen = []
    chosen_ids = set()
    for category in ("Goggins", "Longevity"):
        category_candidates = [v for v in candidates if v["guess"] == category]
        category_candidates.sort(key=lambda video: -video["views"])
        category_chosen = category_candidates[:MAX_PER_CATEGORY]
        category_ids = {video["id"] for video in category_chosen}
        category_chosen.extend(
            video for video in category_candidates
            if video["published"] >= fresh_cutoff and video["id"] not in category_ids
        )
        chosen.extend(category_chosen)
        chosen_ids.update(video["id"] for video in category_chosen)
        print(f"  {category}: {len(category_chosen)} candidates selected.")

    print(f"{len(chosen)} video candidates sent to Gemini.")

    # --- Videos, step C: Gemini judges ---
    labels = load_labels()
    # Earlier runs may have rejected Longevity candidates before category-balanced selection.
    # Reconsider those now that the candidate pool is balanced and includes the search category.
    for video in chosen:
        if video["guess"] == "Longevity":
            previous = labels.get(video["id"], {})
            if previous and previous.get("keep") is False:
                labels.pop(video["id"], None)
    gemini_ok = judge_all(chosen, labels, judge_videos, VIDEO_BATCH)
    if not gemini_ok:
        print("  Gemini could not finish video judging. Unjudged videos will NOT be added.")
        print("  Previously saved videos and previously judged items will be retained.")
    save_labels(labels)

    newly_kept_videos = []
    for video in chosen:
        # Only add videos with an explicit Gemini decision. Never bulk-add unreviewed results.
        label = labels.get(video["id"])
        if not label or not label.get("keep"):
            continue
        # Keep the category assigned from search provenance. Gemini decides relevance,
        # while this prevents one section's videos being mislabeled into the other.
        category = video["guess"]
        newly_kept_videos.append({
            "id": video["id"],
            "title": video["title"],
            "channel": video["channel"],
            "seconds": video["seconds"],
            "views": video["views"],
            "published": video["published"],
            "category": category,
            "topic": label.get("topic", ""),
        })

    # Keep older saved videos even if they were not returned by today's search.
    videos = merge_by_id(existing_videos, newly_kept_videos)
    videos.sort(key=lambda video: (-int(video.get("views", 0)), video.get("published", "")))
    write_json_atomic(videos_path, videos)

    goggins_count = sum(1 for video in videos if video.get("category") == "Goggins")
    longevity_count = sum(1 for video in videos if video.get("category") == "Longevity")
    new_videos = sum(
        1 for video in videos
        if (video.get("published") or "") >= fresh_cutoff
    )
    print(
        f"\\nSaved library: {len(videos)} videos "
        f"({goggins_count} Goggins, {longevity_count} Longevity)."
    )
    print(f"  {new_videos} were published in the last {FRESH_DAYS} days.")

    # --- Papers ---
    print()
    newly_kept_papers = []
    paper_candidates_count = 0
    try:
        paper_candidates = fetch_papers()[:MAX_PAPERS_FOR_GEMINI]
        paper_candidates_count = len(paper_candidates)
        if paper_candidates and gemini_ok:
            gemini_ok = judge_all(paper_candidates, labels, judge_papers, PAPER_BATCH)
        elif paper_candidates:
            print("Skipping paper judging because Gemini had a problem; saved papers are retained.")

        if gemini_ok:
            for paper in paper_candidates:
                label = labels.get(paper["id"])
                if not label or not label.get("keep"):
                    continue
                if label.get("cat") not in ("Longevity", "Toughness") or not label.get("summary"):
                    continue
                newly_kept_papers.append({
                    "id": paper["id"],
                    "title": paper["title"],
                    "authors": paper["authors"],
                    "journal": paper["journal"],
                    "date": paper["date"],
                    "url": paper["url"],
                    "preprint": paper["preprint"],
                    "category": label["cat"],
                    "summary": label["summary"],
                    "evidence": label.get("evidence", "other"),
                })
    except Exception as exc:
        print(f"Paper discovery failed; existing papers will be retained: {exc}")

    papers = merge_by_id(existing_papers, newly_kept_papers)
    papers.sort(key=lambda paper: paper.get("date", ""), reverse=True)
    write_json_atomic(papers_path, papers)
    save_labels(labels)
    print(f"Saved research library: {len(papers)} papers ({paper_candidates_count} candidates this run).")

    # Status is written last, after the data files have been saved successfully.
    status = {
        "updated": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "videos": len(videos),
        "new_videos": new_videos,
        "papers": len(papers),
        "video_candidates": len(candidates),
        "paper_candidates": paper_candidates_count,
        "gemini_ok": bool(gemini_ok),
    }
    write_json_atomic(status_path, status)
    print("Pipeline completed successfully.")


if __name__ == "__main__":
    main()
