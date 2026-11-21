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
  5. Research tab: open-access MATH and AI papers from arXiv, ranked by quality signals and
     filtered by Gemini down to the substantive ones.

Run it any time:   py pipeline.py        You need:   pip install requests
"""
import os
import re
import json
import time
import datetime
import requests
import xml.etree.ElementTree as ET

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
GOGGINS_CHANNELS = []   # Goggins videos come from the searches below. Only add a handle here if you are
                        # sure it is a channel where EVERY video is Goggins (they are kept automatically).
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
PAPER_CALLS_RESERVED = 4         # always keep this many for papers
VIDEO_BATCH = 50                 # videos judged per request
PAPER_BATCH = 15                 # papers judged per request
PAUSE_BETWEEN_CALLS = 13         # seconds between requests (free tier = ~5 per minute)

# --- Research: open-access math + AI papers from arXiv (free, no key) ---
PAPER_KEEP_DAYS = 120            # show papers up to 120 days old
FIRST_RUN_MULTIPLIER = 4         # the very first run looks back 4x further, to fill the tab quicker
ARXIV_PAUSE = 3                  # arXiv asks for 3 seconds between requests
# feed name -> (arXiv search, days to look back, max papers to fetch)
PAPER_FEEDS = {
    "Math": ("cat:math.NT OR cat:math.CO OR cat:math.LO OR cat:math.PR OR cat:math.AG OR cat:math.GR "
             "OR cat:math.CA OR cat:math.DS OR cat:math.AT OR cat:math.RT", 2, 200),
    "AI": ("cat:cs.AI OR cat:cs.LG OR cat:cs.CL OR cat:stat.ML", 1, 200),
    "Proofs": ('(cat:cs.LO OR cat:cs.AI OR cat:cs.LG OR cat:cs.CL OR cat:math.LO) AND '
               '(abs:"theorem proving" OR abs:"formal proof" OR abs:Lean OR abs:autoformalization '
               'OR abs:"proof assistant" OR abs:"formal verification" OR abs:"mathematical reasoning")', 14, 100),
}
# Papers the authors say are accepted at, or published in, these get a badge and a ranking boost.
GOOD_VENUES = ["NeurIPS", "ICML", "ICLR", "ACL", "EMNLP", "NAACL", "CVPR", "AAAI", "COLT", "STOC", "FOCS",
               "SODA", "LICS", "ITP", "CAV", "JACM", "Annals of Mathematics", "Inventiones", "Acta Mathematica",
               "Journal of the American Mathematical Society", "Duke Mathematical Journal",
               "Forum of Mathematics", "Compositio", "Advances in Mathematics"]
# Words in a title/abstract that hint at substance. Used only to decide what Gemini looks at first.
SIGNALS = {"we prove": 3, "we resolve": 4, "we settle": 4, "we disprove": 4, "counterexample": 3,
           "conjecture": 2, "open problem": 3, "new proof": 3, "first proof": 4, "theorem": 2,
           "formalized": 3, "formalised": 3, "lean 4": 3, "machine-checked": 3, "state-of-the-art": 1,
           "scaling law": 2, "mathematical reasoning": 2, "we show that": 1}

TODAY = datetime.date.today()
STATE = {"yt_blocked": False}


class QuotaExceeded(Exception):
    pass


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
    rot = lambda xs: xs[shift % len(xs):] + xs[:shift % len(xs)] if xs else xs   # a different start each day
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
# 3. RESEARCH PAPER HELPERS  (arXiv: free, open access, no key)
# ---------------------------------------------------------------

ARXIV_API = "https://export.arxiv.org/api/query"
NS = {"a": "http://www.w3.org/2005/Atom", "x": "http://arxiv.org/schemas/atom"}


def find_venue(comment, journal_ref):
    """Only trust a venue if the authors say the paper was accepted/published there."""
    text = f"{comment} {journal_ref}"
    if not (journal_ref or re.search(r"accepted|to appear|appears|published|oral|spotlight|camera", comment, re.I)):
        return ""
    for v in GOOD_VENUES:
        if re.search(r"\b" + re.escape(v) + r"\b", text):
            return v
    return ""


def paper_score(p):
    """A rough 'worth reading first' score. It only decides the order Gemini looks in."""
    text = (p["title"] + " " + p["abstract"]).lower()
    score = 5 if p["venue"] else 0
    score += sum(pts for word, pts in SIGNALS.items() if word in text)
    score += 2 if p["code"] else 0
    return score


def fetch_arxiv(feed, query, days, max_results):
    """Newest papers of one feed. Returns a list of paper dicts."""
    now = datetime.datetime.now(datetime.timezone.utc)
    start = (now - datetime.timedelta(days=days)).strftime("%Y%m%d0000")
    params = {"search_query": f"({query}) AND submittedDate:[{start} TO {now.strftime('%Y%m%d2359')}]",
              "sortBy": "submittedDate", "sortOrder": "descending", "start": 0, "max_results": max_results}
    content = None
    for attempt in range(2):
        try:
            r = requests.get(ARXIV_API, params=params, timeout=90)
            if r.status_code == 200:
                content = r.content
                break
            print(f"  arXiv said {r.status_code}. Trying again...")
        except Exception as e:
            print("  arXiv problem:", e)
        time.sleep(5)
    if content is None:
        return []
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return []

    papers = []
    for e in root.findall("a:entry", NS):
        try:
            raw_id = e.findtext("a:id", "", NS)
            if "/abs/" not in raw_id:          # arXiv sends an 'error' entry when something is wrong
                continue
            arxiv_id = re.sub(r"v\d+$", "", raw_id.split("/abs/")[1])
            title = " ".join(e.findtext("a:title", "", NS).split())
            abstract = " ".join(e.findtext("a:summary", "", NS).split())
            comment = " ".join(e.findtext("x:comment", "", NS).split())
            journal_ref = " ".join(e.findtext("x:journal_ref", "", NS).split())
            primary = (e.find("x:primary_category", NS).get("term") if e.find("x:primary_category", NS) is not None else "")
            authors = [" ".join(a.findtext("a:name", "", NS).split()) for a in e.findall("a:author", NS)]
            code = re.search(r"https?://(?:www\.)?(?:github\.com|gitlab\.com)/[^\s,;)\]}>\"']+",
                             comment + " " + abstract)
            venue = find_venue(comment, journal_ref)
            p = {
                "id": f"arxiv:{arxiv_id}", "feed": feed, "title": title, "abstract": abstract,
                "primary": primary, "venue": venue, "code": code.group(0).rstrip(".") if code else "",
            }
            p["score"] = paper_score(p)
            p["meta"] = {
                "title": title,
                "authors": ", ".join(authors[:3]) + (" et al." if len(authors) > 3 else ""),
                "journal": "arXiv " + primary + (f" · {venue}" if venue else ""),
                "date": e.findtext("a:published", "", NS)[:10],
                "url": f"https://arxiv.org/abs/{arxiv_id}",
                "pdf": f"https://arxiv.org/pdf/{arxiv_id}",
                "code": p["code"],
            }
            papers.append(p)
        except (AttributeError, IndexError, ValueError):
            continue
    print(f"  {len(papers)} papers fetched.")
    return papers


def rank_for_judging(by_feed):
    """Best-scoring first, with the feeds taking turns so Math, AI and Proofs all get looked at."""
    lists = [sorted(v, key=lambda p: -p["score"]) for v in by_feed.values()]
    out = []
    for i in range(max((len(l) for l in lists), default=0)):
        for l in lists:
            if i < len(l):
                out.append(l[i])
    return out


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

PAPER_PROMPT = """You are curating a research section for a reader who wants HIGH-LEVEL work in mathematics
(proofs, number theory, combinatorics, logic, ...) and AI (reasoning, theory, formal theorem proving,
major advances). All papers below are open-access arXiv preprints, so quality varies a lot.
KEEP only papers that look substantive and significant: new theorems or proofs, resolved or advanced
conjectures, rigorous theory, formalized or machine-checked proofs (Lean etc.), important new methods,
strong results from credible work. REJECT incremental tweaks, narrow applications, hype, vague or
low-information abstracts.
For kept papers also write:
- "cat": "Math", "AI" or "Proofs" ("Proofs" = formal, machine-checked proofs or AI theorem proving)
- "summary": ONE plain-English sentence (max 35 words) using ONLY what the abstract says. No hype,
  and never add claims that are not in the abstract.
- "kind": one of "new theorem", "new proof", "survey", "new method", "theory", "benchmark", "formal proof", "other"
- "level": "advanced" or "expert"
Return ONLY a JSON array, one object per paper, like this:
[{"i": 0, "keep": true, "cat": "Math", "summary": "...", "kind": "new theorem", "level": "expert"}]

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
    lines = [f"[{i}] ({p['feed']}) {p['title']} | {p['primary']} | {p['abstract'][:600]}"
             for i, p in enumerate(batch)]
    verdicts = {x["i"]: x for x in call_gemini(PAPER_PROMPT + "\n".join(lines))}
    labels = []
    for i in range(len(batch)):
        x = verdicts.get(i)
        if x and x.get("keep") and x.get("cat") in ("Math", "AI", "Proofs") and x.get("summary"):
            labels.append({"keep": True, "cat": x["cat"], "summary": x["summary"],
                           "kind": x.get("kind", "other"), "level": x.get("level", "advanced")})
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
    odd = 0
    for item in details:
        try:
            src = found[item["id"]]
            # Live streams and premieres can be missing a length or statistics, so use .get()
            seconds = duration_to_seconds((item.get("contentDetails") or {}).get("duration"))
            if seconds < MIN_SECONDS:
                continue
            guess = guess_category(item, src["cat"]) if src["kind"] == "search" else src["cat"]
            if guess is None:
                continue
            snippet = item["snippet"]
            candidates.append({
                "id": item["id"], "title": snippet["title"],
                "channel": snippet["channelTitle"],
                "description": snippet.get("description", ""),
                "published": snippet["publishedAt"][:10],
                "auto": src["kind"] == "goggins_channel",
                "meta": {"title": snippet["title"], "channel": snippet["channelTitle"],
                         "seconds": seconds,
                         "views": int((item.get("statistics") or {}).get("viewCount", 0)),
                         "published": snippet["publishedAt"][:10]},
            })
        except (KeyError, ValueError, TypeError):
            odd += 1          # one strange video should never stop the whole run
    if odd:
        print(f"  (skipped {odd} videos with missing details)")

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

    # --- Papers (arXiv) ---
    print()
    first_run = not any(k.startswith("arxiv:") for k in labels)
    by_feed, seen = {}, set()
    for feed, (query, days, max_results) in PAPER_FEEDS.items():
        print(f"Searching arXiv: {feed}")
        got = fetch_arxiv(feed, query, days * (FIRST_RUN_MULTIPLIER if first_run else 1), max_results)
        by_feed[feed] = []
        for p in got:
            if p["id"] in seen:
                continue
            seen.add(p["id"])
            label = labels.get(p["id"])
            if label is None:
                by_feed[feed].append(p)
            elif label.get("keep"):
                remember(labels, p, label)          # refresh details
        time.sleep(ARXIV_PAUSE)
    todo_p = rank_for_judging(by_feed)
    print(f"{len(todo_p)} new papers could be judged (best-looking first).")
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
        if key.startswith("arxiv:"):
            if lab.get("date", "") >= paper_cutoff:
                papers.append({"id": key, "title": lab["title"], "authors": lab.get("authors", ""),
                               "journal": lab.get("journal", "arXiv"), "date": lab["date"], "url": lab["url"],
                               "pdf": lab.get("pdf", ""), "code": lab.get("code", ""),
                               "category": lab["cat"], "summary": lab["summary"],
                               "kind": lab.get("kind", ""), "level": lab.get("level", "")})
        elif ":" in key or "channel" not in lab:
            continue                       # leftovers from older versions
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
