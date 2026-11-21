#!/usr/bin/env python3
"""
GogginsTube pipeline v4  (built around the FREE limits)
-------------------------------------------------------
The free limits, in plain words:
  * YouTube: only about 100 SEARCHES per day (every run counts, even test runs on your laptop).
    Looking up videos or reading a channel's uploads is nearly free.
  * Gemini (free): only a limited number of requests per day.

So this version:
  1. Reads the uploads of a few chosen CHANNELS first (almost free, gives the newest videos)
  2. Uses only a small, fixed number of searches to discover extra videos
  3. Asks Gemini to judge videos and papers in big batches, with a hard cap on requests
  4. Remembers everything in labels.json, so the site only ever GROWS. A run that hits a limit
     never makes the site smaller; the next run carries on where this one stopped.
  5. Research papers: only from a list of top journals, and no preprints.

Run it any time:   py pipeline.py        You need:   pip install requests
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

# --- YouTube: channels (cheap) ---
# Handles look like @name (the part after youtube.com/ in the channel's link).
# If a handle is wrong, the script just says "Couldn't find channel" and moves on.
GOGGINS_CHANNELS = ["@davidgoggins"]                  # everything here is kept automatically
LONGEVITY_CHANNELS = ["@PeterAttiaMD", "@hubermanlab", "@FoundMyFitness"]   # Gemini picks the relevant videos

# --- YouTube: searches (expensive: 1 search = 1 of your ~100 per day) ---
MAX_SEARCHES_PER_RUN = 20        # leaves room for a few extra runs on the same day
GOGGINS_SEARCHES = [
    "David Goggins interview",
    "David Goggins podcast",
    "David Goggins motivation speech",
    "David Goggins workout",
]
LONGEVITY_SEARCHES = [
    "longevity science explained",
    "healthspan how to live longer",
    "David Sinclair aging",
    "Bryan Johnson longevity",
    "zone 2 cardio VO2 max longevity",
    "Valter Longo fasting longevity",
]
LONGEVITY_WORDS = ["longevity", "aging", "ageing", "healthspan", "lifespan",
                   "live longer", "anti-aging", "vo2 max", "blue zone"]

CHANNEL_DEEP_PAGES = 6           # first time a channel is read: up to 6 x 50 = 300 videos
FRESH_DAYS = 30                  # "new" = published in the last 30 days
MIN_SECONDS = 240                # skip anything shorter than 4 minutes (kills Shorts)

# --- Gemini (free tier is limited, so we cap requests per run) ---
MAX_GEMINI_CALLS = 14            # total requests per run (videos + papers)
PAPER_CALLS_RESERVED = 3         # always keep this many for papers
VIDEO_BATCH = 50                 # videos judged per request
PAPER_BATCH = 15                 # papers judged per request
PAUSE_BETWEEN_CALLS = 13         # seconds between requests (free tier = ~5 per minute)

# --- Research papers ---
NEW_PAPER_DAYS = 90              # look at papers first published in the last 90 days
PAPER_KEEP_DAYS = 365            # show papers up to a year old
PAPER_SEARCHES = {
    "Longevity": 'longevity OR healthspan OR geroscience OR "biological age" OR "lifespan extension" '
                 'OR "epigenetic clock" OR "cellular senescence"',
    "Toughness": '"mental toughness" OR "psychological resilience" OR "ultra-endurance" '
                 'OR "perceived exertion" OR "grit"',
}
# Only papers from these journals are shown. Add or remove names as you like.
# (Names follow the PubMed style. The run log lists skipped journals so you can add ones you want.)
TOP_JOURNALS = [
    # big general science and medicine
    "Nature", "Science (New York, N.Y.)", "Science", "Cell", "Lancet (London, England)", "The Lancet",
    "The New England journal of medicine", "JAMA", "BMJ (Clinical research ed.)", "BMJ",
    "Nature medicine", "Proceedings of the National Academy of Sciences of the United States of America",
    "Annals of internal medicine", "JAMA internal medicine",
    # ageing and metabolism
    "Nature aging", "Aging cell", "GeroScience", "The Lancet. Healthy longevity", "Cell metabolism",
    "Nature metabolism", "Ageing research reviews",
    "The journals of gerontology. Series A, Biological sciences and medical sciences",
    # heart and fitness
    "Circulation", "European heart journal", "Journal of the American College of Cardiology",
    "British journal of sports medicine", "Sports medicine (Auckland, N.Z.)",
    "Medicine and science in sports and exercise",
    # mental toughness and psychology
    "Psychological science", "Psychological bulletin", "Nature human behaviour",
    "Journal of personality and social psychology",
]

TODAY = datetime.date.today()
STATE = {"yt_blocked": False}


class QuotaExceeded(Exception):
    pass


def norm_journal(name):
    """'The Lancet. Healthy longevity' -> 'lancet healthy longevity' (so spelling quirks still match)."""
    n = re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).strip()
    return re.sub(r"^the ", "", n)


JOURNAL_SET = {norm_journal(j) for j in TOP_JOURNALS}


# ---------------------------------------------------------------
# 2. YOUTUBE HELPERS
# ---------------------------------------------------------------

def is_quota_error(err):
    reasons = [e.get("reason", "") for e in err.get("errors", [])]
    return (any(r in ("quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded") for r in reasons)
            or "quota" in (err.get("message") or "").lower())


def yt_get(endpoint, params):
    """Call YouTube. Raises QuotaExceeded when today's free limit is used up."""
    data = requests.get(f"https://www.googleapis.com/youtube/v3/{endpoint}",
                        params=dict(params, key=YOUTUBE_KEY), timeout=30).json()
    err = data.get("error")
    if err:
        if is_quota_error(err):
            raise QuotaExceeded(err.get("message"))
        print("  YouTube said:", err.get("message"))
        return None
    return data


def resolve_channel(handle, cache):
    """Turn @handle into that channel's 'uploads' list (cached, so it costs nothing next time)."""
    if handle in cache:
        return cache[handle]
    data = yt_get("channels", {"part": "contentDetails,snippet", "forHandle": handle})
    items = (data or {}).get("items", [])
    if not items:
        print(f"  Couldn't find channel {handle}. Check the spelling in pipeline.py.")
        return None
    cache[handle] = {"uploads": items[0]["contentDetails"]["relatedPlaylists"]["uploads"],
                     "name": items[0]["snippet"]["title"], "deep": False}
    return cache[handle]


def channel_video_ids(info):
    """Newest uploads of a channel. Deep scan the first time, just the newest 50 after that."""
    pages = 1 if info.get("deep") else CHANNEL_DEEP_PAGES
    ids, token = [], None
    for _ in range(pages):
        params = {"part": "contentDetails", "playlistId": info["uploads"], "maxResults": 50}
        if token:
            params["pageToken"] = token
        data = yt_get("playlistItems", params)
        if not data:
            break
        ids += [x["contentDetails"]["videoId"] for x in data.get("items", [])]
        token = data.get("nextPageToken")
        if not token:
            break
    info["deep"] = True
    return ids


def search_youtube(query, fresh):
    """One search = one of your ~100 daily searches. fresh=True means newest first, recent only."""
    params = {"part": "snippet", "q": query, "type": "video", "maxResults": 50}
    if fresh:
        cutoff = TODAY - datetime.timedelta(days=FRESH_DAYS)
        params["order"] = "date"
        params["publishedAfter"] = cutoff.isoformat() + "T00:00:00Z"
    data = yt_get("search", params)
    return [x["id"]["videoId"] for x in (data or {}).get("items", [])]


def get_details(video_ids):
    """Title, channel, length, views for each video (50 per request, nearly free)."""
    details = []
    for start in range(0, len(video_ids), 50):
        data = yt_get("videos", {"part": "snippet,contentDetails,statistics",
                                 "id": ",".join(video_ids[start:start + 50])})
        details.extend((data or {}).get("items", []))
    return details


def duration_to_seconds(iso):
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso or "")
    if not m:
        return 0
    hours, minutes, seconds = (int(x or 0) for x in m.groups())
    return hours * 3600 + minutes * 60 + seconds


def guess_category(item, searched_for):
    """Quick keyword check for SEARCH results. Returns 'Goggins', 'Longevity', or None (= junk)."""
    title = item["snippet"]["title"].lower()
    channel = item["snippet"]["channelTitle"].lower()
    text = title + " " + item["snippet"].get("description", "").lower()
    if "goggins" in title or "goggins" in channel:
        return "Goggins"
    if searched_for == "Longevity" and any(w in text for w in LONGEVITY_WORDS):
        return "Longevity"
    return None


def build_search_plan():
    """Newest-first searches come first (they find today's new videos), then all-time-best ones.
    Goggins and Longevity take turns so that neither one gets starved if the budget runs out."""
    g, l = GOGGINS_SEARCHES, LONGEVITY_SEARCHES
    shift = TODAY.toordinal()
    rot = lambda xs: xs[shift % len(xs):] + xs[:shift % len(xs)]    # a different starting point each day
    plan = []
    for fresh in (True, False):
        gg, ll = (g, l) if fresh else (rot(g), rot(l))
        for i in range(max(len(gg), len(ll))):
            if i < len(gg):
                plan.append((gg[i], "Goggins", fresh))
            if i < len(ll):
                plan.append((ll[i], "Longevity", fresh))
    return plan


# ---------------------------------------------------------------
# 3. RESEARCH PAPER HELPERS  (Europe PMC: free, no key, no preprints)
# ---------------------------------------------------------------

def strip_tags(text):
    return re.sub(r"<[^>]+>", "", text or "").strip()


def fetch_papers():
    """Recent papers from top journals only. Returns a list, newest first."""
    since = (TODAY - datetime.timedelta(days=NEW_PAPER_DAYS)).isoformat()
    journal_clause = " OR ".join(f'JOURNAL:"{j}"' for j in TOP_JOURNALS)
    papers, skipped = {}, {}
    for category, terms in PAPER_SEARCHES.items():
        print("Searching papers:", category)
        # SRC:MED = peer-reviewed journals indexed by PubMed (this leaves out preprints)
        base = f"({terms}) AND SRC:MED AND (FIRST_PDATE:[{since} TO {TODAY.isoformat()}]) AND HAS_ABSTRACT:y"
        results = []
        for query in (base + f" AND ({journal_clause})", base):     # second one is a fallback
            try:
                data = requests.get("https://www.ebi.ac.uk/europepmc/webservices/rest/search", params={
                    "query": query, "format": "json", "resultType": "core", "pageSize": 100,
                }, timeout=60).json()
            except Exception as e:
                print("  Paper search problem:", e)
                continue
            results = data.get("resultList", {}).get("result", [])
            if results:
                break
        for x in results:
            journal = x.get("journalTitle") or ""
            if x.get("source") == "PPR" or not x.get("title"):
                continue
            if norm_journal(journal) not in JOURNAL_SET:
                skipped[journal] = skipped.get(journal, 0) + 1
                continue
            pid = f"paper:{x.get('source', '')}:{x.get('id', '')}"
            if pid in papers:
                continue
            doi = x.get("doi")
            url = f"https://doi.org/{doi}" if doi else f"https://europepmc.org/article/{x.get('source')}/{x.get('id')}"
            authors = [a.strip() for a in (x.get("authorString") or "").rstrip(".").split(",") if a.strip()]
            title = strip_tags(x["title"])
            date = x.get("firstPublicationDate") or str(x.get("pubYear", ""))
            papers[pid] = {
                "id": pid, "title": title, "journal": journal, "date": date,
                "abstract": strip_tags(x.get("abstractText")),
                "meta": {"title": title, "journal": journal, "date": date, "url": url,
                         "authors": ", ".join(authors[:3]) + (" et al." if len(authors) > 3 else "")},
            }
    result = sorted(papers.values(), key=lambda p: p["date"], reverse=True)
    print(f"  {len(result)} papers from top journals.")
    if skipped:
        top = sorted(skipped, key=lambda j: -skipped[j])[:5]
        print(f"  Skipped {sum(skipped.values())} from other journals (e.g. {'; '.join(top)}).")
    return result


# ---------------------------------------------------------------
# 4. GEMINI HELPERS
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
All papers below come from respected journals. For each numbered paper decide whether to KEEP it: keep
papers clearly about those themes that would interest a curious, non-expert reader. Reject papers that
are off-topic or only mention the theme in passing.
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
        try:
            data = r.json()
        except ValueError:
            data = {}
        if r.status_code == 429:
            msg = (data.get("error", {}).get("message") or "").lower()
            if "day" in msg or "daily" in msg:
                raise RuntimeError("Gemini's free DAILY limit is used up. Videos and papers not judged yet "
                                   "will be done by the next run.")
            print("  Gemini says slow down. Waiting 40 seconds...")
            time.sleep(40)
            continue
        if r.status_code != 200:
            raise RuntimeError(data.get("error", {}).get("message", str(data)))
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        return json.loads(re.search(r"\[.*\]", text, re.S).group(0))
    raise RuntimeError("Gemini kept saying slow down (free limit). The next run will carry on.")


def judge_videos(batch):
    lines = []
    for i, v in enumerate(batch):
        desc = v["description"][:150].replace("\n", " ")
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
    lines = [f"[{i}] {p['title']} | {p['journal']} | {p['abstract'][:700]}" for i, p in enumerate(batch)]
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


# labels.json is the site's memory: every verdict, plus the details of everything we kept.
def load_labels():
    if os.path.exists("labels.json"):
        with open("labels.json") as f:
            return json.load(f)
    return {}


def save_labels(labels):
    with open("labels.json", "w") as f:
        json.dump(labels, f)


def remember(labels, item, label):
    """Store a verdict. For kept items we also store their details, so the site can always rebuild itself."""
    if label.get("keep"):
        label.update(item["meta"])
    labels[item["id"]] = label


def judge_all(items, labels, judge, batch_size, max_calls):
    """Send items to Gemini in batches (at most max_calls requests). Returns (ok, requests_used)."""
    calls = 0
    for start in range(0, len(items), batch_size):
        if calls >= max_calls:
            print(f"  {len(items) - start} more will be judged in a later run (protects your free Gemini limit).")
            break
        batch = items[start:start + batch_size]
        try:
            results = judge(batch)
        except Exception as e:
            print("  Gemini problem:", e)
            return False, calls
        calls += 1
        for item, label in zip(batch, results):
            remember(labels, item, label)
        save_labels(labels)                    # saved after every request, so progress is never lost
        print(f"  Gemini judged {min(start + batch_size, len(items))} / {len(items)}")
        time.sleep(PAUSE_BETWEEN_CALLS)
    return True, calls


# ---------------------------------------------------------------
# 5. KEY CHECKS
# ---------------------------------------------------------------

def check_keys():
    STATE["yt_blocked"] = False
    keys = [YOUTUBE_KEY, GEMINI_KEY]
    if not all(isinstance(k, str) and k.strip() and "PASTE_" not in k for k in keys):
        print("Open keys.py and paste BOTH keys in it first. (In GitHub: check your two Secrets.)")
        print('Each line should look like:  YOUTUBE_KEY = "AIza...your key..."')
        return False

    ok = True
    r = requests.get("https://www.googleapis.com/youtube/v3/videos",
                     params={"part": "id", "id": "dQw4w9WgXcQ", "key": YOUTUBE_KEY}, timeout=30).json()
    if "error" in r:
        if is_quota_error(r["error"]):
            print("! YouTube's free daily limit is already used up. Skipping YouTube this run.")
            print("  (It resets at midnight Pacific Time. Papers and your saved videos still update.)")
            STATE["yt_blocked"] = True
        else:
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


def find_out_dir():
    """Where the website files live: the folder that already has videos.json or index.html."""
    for needed in ("videos.json", "index.html"):
        for d in ("site", "docs", "."):
            if os.path.exists(os.path.join(d, needed)):
                return d
    return "site"


# ---------------------------------------------------------------
# 6. MAIN PROGRAM
# ---------------------------------------------------------------

def main():
    if not check_keys():
        return
    labels = load_labels()
    out_dir = find_out_dir()
    os.makedirs(out_dir, exist_ok=True)
    channel_cache = labels.setdefault("_channels", {})
    fresh_cutoff = (TODAY - datetime.timedelta(days=FRESH_DAYS)).isoformat()

    # --- Videos A: channels, then a few searches ---
    found = {}   # video ID -> where it came from
    if not STATE["yt_blocked"]:
        try:
            for handles, kind, cat in ((GOGGINS_CHANNELS, "goggins_channel", "Goggins"),
                                       (LONGEVITY_CHANNELS, "long_channel", "Longevity")):
                for handle in handles:
                    info = resolve_channel(handle, channel_cache)
                    if info:
                        ids = channel_video_ids(info)
                        print(f"Channel {handle}: {len(ids)} videos read")
                        for vid in ids:
                            found.setdefault(vid, {"cat": cat, "kind": kind})
            used = 0
            for query, cat, fresh in build_search_plan():
                if used >= MAX_SEARCHES_PER_RUN:
                    break
                used += 1
                print(f"Search {used}/{MAX_SEARCHES_PER_RUN}: {query}{' [newest]' if fresh else ''}")
                for vid in search_youtube(query, fresh):
                    found.setdefault(vid, {"cat": cat, "kind": "search"})
        except QuotaExceeded as e:
            print("\n! YouTube's free daily limit ran out:", e)
            print("  Nothing is lost. What we already have is kept, and the next run carries on.")

    # --- Videos B: details, then keep only sensible candidates ---
    try:
        details = get_details(list(found))
    except QuotaExceeded:
        details = []
    print(f"\n{len(found)} videos found this run.")
    candidates = []
    for item in details:
        src = found[item["id"]]
        seconds = duration_to_seconds(item["contentDetails"]["duration"])
        if seconds < MIN_SECONDS:
            continue
        guess = guess_category(item, src["cat"]) if src["kind"] == "search" else src["cat"]
        if guess is None:
            continue
        candidates.append({
            "id": item["id"], "title": item["snippet"]["title"],
            "channel": item["snippet"]["channelTitle"],
            "description": item["snippet"].get("description", ""),
            "published": item["snippet"]["publishedAt"][:10],
            "auto": src["kind"] == "goggins_channel",
            "meta": {"title": item["snippet"]["title"], "channel": item["snippet"]["channelTitle"],
                     "seconds": seconds, "views": int(item["statistics"].get("viewCount", 0)),
                     "published": item["snippet"]["publishedAt"][:10]},
        })

    todo = []
    for v in candidates:
        label = labels.get(v["id"])
        if label is None:
            if v["auto"]:      # videos from Goggins' own channel: no AI needed
                remember(labels, v, {"keep": True, "cat": "Goggins", "topic": ""})
            else:
                todo.append(v)
        elif label.get("keep"):
            remember(labels, v, label)          # refresh view counts, fill in details
    todo.sort(key=lambda v: (v["published"] < fresh_cutoff, -v["meta"]["views"]))   # new ones first

    # --- Videos C: Gemini judges (capped) ---
    print(f"{len(todo)} new videos need Gemini's verdict.")
    gemini_ok, calls = judge_all(todo, labels, judge_videos, VIDEO_BATCH,
                                 max_calls=MAX_GEMINI_CALLS - PAPER_CALLS_RESERVED)

    # --- Papers ---
    print()
    todo_p = []
    for p in fetch_papers():
        label = labels.get(p["id"])
        if label is None:
            todo_p.append(p)
        elif label.get("keep"):
            remember(labels, p, label)
    print(f"{len(todo_p)} new papers need Gemini's verdict.")
    if todo_p and gemini_ok:
        judge_all(todo_p, labels, judge_papers, PAPER_BATCH, max_calls=MAX_GEMINI_CALLS - calls)
    elif todo_p:
        print("Skipping papers because Gemini had a problem. The next run will do them.")
    save_labels(labels)

    # --- Build the site's files from the memory (so the site never shrinks) ---
    videos, papers = [], []
    paper_cutoff = (TODAY - datetime.timedelta(days=PAPER_KEEP_DAYS)).isoformat()
    for key, lab in labels.items():
        if key.startswith("_") or not lab.get("keep") or "title" not in lab:
            continue
        if key.startswith("paper:"):
            if norm_journal(lab.get("journal")) in JOURNAL_SET and lab.get("date", "") >= paper_cutoff:
                papers.append({"id": key, "title": lab["title"], "authors": lab.get("authors", ""),
                               "journal": lab["journal"], "date": lab["date"], "url": lab["url"],
                               "category": lab["cat"], "summary": lab["summary"],
                               "evidence": lab.get("evidence", "other")})
        else:
            videos.append({"id": key, "title": lab["title"], "channel": lab["channel"],
                           "seconds": lab["seconds"], "views": lab["views"], "published": lab["published"],
                           "category": lab["cat"], "topic": lab.get("topic", "")})
    videos.sort(key=lambda v: -v["views"])
    papers.sort(key=lambda p: p["date"], reverse=True)
    papers = papers[:150]

    new_videos = sum(1 for v in videos if v["published"] >= fresh_cutoff)
    for name, data in (("videos.json", videos), ("papers.json", papers)):
        with open(os.path.join(out_dir, name), "w") as f:
            json.dump(data, f)
    status = {"updated": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
              "videos": len(videos), "new_videos": new_videos, "papers": len(papers)}
    with open(os.path.join(out_dir, "status.json"), "w") as f:
        json.dump(status, f)

    goggins = sum(1 for v in videos if v["category"] == "Goggins")
    print(f"\nSite now has {len(videos)} videos ({goggins} Goggins, {len(videos) - goggins} Longevity), "
          f"{new_videos} new, and {len(papers)} papers. Saved in {out_dir}/")


if __name__ == "__main__":
    main()
