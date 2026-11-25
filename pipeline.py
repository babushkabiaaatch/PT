#!/usr/bin/env python3
"""
GogginsTube pipeline v5
-----------------------
Sections: Goggins, Longevity, Russian (learning Russian), and Cool Stuff (curated maths, science, computing and engineering videos).

What is new in v5, in plain words
  1. RUSSIAN section: videos AND playlists from learn-Russian channels and searches. Gemini labels each
     one with a level (A1 ... C1) and throws away anything that is not made for learners.
  2. BEST-ONLY ranking: every video gets a 0-100 quality score built from signals that are hard to fake:
       - like rate         (likes / views, smoothed so tiny videos can't cheat)
       - reach + momentum  (total views, and views per day)
       - Gemini quality    (1-5: substance, original source, not clickbait / compilation / ad)
       - channel trust     (a list of channels you trust, a penalty for "motivation" re-upload farms)
       - length fit        (a 4-minute "podcast" or a 6-hour stream is rarely the best pick)
     The site presents videos newest-first by default; quality scores remain available for curation but never hide fresh uploads.
  3. SUMMARIES: written to site/summaries.json.  Source, best first:
       transcript (youtube-transcript-api)  ->  Gemini watching the video (short videos)  ->  description.
     Chapters ("key moments") are read free from video descriptions.
  4. SAFE WITH YOUR EXISTING DATA: site/videos.json, papers.json, labels.json, channels.json and quota.json
     are loaded first and only ever grow. A bad run can never empty the site.

Free limits (unchanged): YouTube ~100 searches a day (a search costs 100 of 10,000 units, almost everything
else costs 1) and Gemini's free tier. Every request is capped and the run carries on tomorrow.

Run it:   python pipeline.py            (needs:  pip install requests youtube-transcript-api)
Rescore without any network or keys:   python pipeline.py --rebuild
"""
import os
import re
import sys
import json
import time
import math
import bisect
import statistics
import datetime
import requests
import xml.etree.ElementTree as ET

try:
    from keys import YOUTUBE_KEY, GEMINI_KEY          # on your laptop, keys live in keys.py
except ImportError:                                   # in the cloud (GitHub) they come from "secrets"
    YOUTUBE_KEY = os.environ.get("YOUTUBE_API_KEY", "")
    GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")

# ---------------------------------------------------------------
# 1. SETTINGS
# ---------------------------------------------------------------

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "").strip() or "gemini-3.5-flash-lite"

CATS = ("Goggins", "Longevity", "Russian", "Cool Stuff")

# --- YouTube channels (cheap: 1 unit per 50 videos). Handles look like @name. ---
# A wrong handle just prints "Couldn't find channel" once and is retried after two weeks.
CHANNELS = {
    "Goggins": [],
    "Longevity": ["@PeterAttiaMD", "@hubermanlab", "@FoundMyFitness", "@bryanjohnson", "@SiimLand","@ARDD","@Aging-US","@DaveAspreyBPR","optispan","@quinnstillsonmd"
],
    "Russian": ["@RealRussianClub", "@russianpod101", "@RussianLevel1", "@BeFluentinRussian",
                "@RussianwithAnastasia", "@RussianwithDasha", "@LearnRussianStanislavAcademy",
                "@BoostYourRussian", "@EasyRussianVideos", "@RussianwithMax", "@ComprehensibleRussian"],
    "Cool Stuff": ["@numberphile", "@computerphile", "@veritasium", "@3blue1brown",
                   "@standupmaths", "@mattparker", "@minutephysics", "@PBSInfiniteSeries",
                   "@Fermilab", "@SmarterEveryDay", "@SteveMould", "@PracticalEngineeringChannel",
                   "@BenEater", "@SebastianLague", "@WelchLabsVideo", "@TheRoyalInstitution"],
}

# --- YouTube searches (expensive: 100 units each) ---
MAX_SEARCHES_PER_RUN = 14
SEARCH_UNITS_CAP = 9000             # never start a search past this many units used today
SEARCHES = {
    "Goggins": ["David Goggins interview", "David Goggins podcast", "David Goggins speech",
                "David Goggins workout", "David Goggins documentary"],
    "Longevity": ["longevity science explained", "healthspan how to live longer", "David Sinclair aging",
                  "zone 2 cardio VO2 max longevity", "Valter Longo fasting longevity",
                  "strength training healthy aging", "metabolic health insulin resistance",
                  "aging biology geroscience"],
    "Russian": ["learn Russian for beginners A1", "Russian grammar explained", "Russian listening practice A2 B1",
                "learn Russian intermediate B1 B2", "Russian vocabulary lesson", "Russian alphabet pronunciation lesson",
                "Russian comprehensible input"],
    "Cool Stuff": ["Numberphile mathematics explained", "Computerphile computer science explained",
                   "Veritasium science engineering mathematics", "3Blue1Brown visual mathematics",
                   "Stand-up Maths mathematical puzzles", "Fermilab particle physics explained",
                   "Ben Eater computer engineering build", "Practical Engineering infrastructure science",
                   "SmarterEveryDay engineering science experiment", "Steve Mould physics engineering"],
}
PLAYLIST_SEARCHES = ["learn Russian full course playlist beginner", "Russian A1 A2 B1 B2 course playlist",
                     "Russian grammar lessons playlist", "Russian listening practice playlist"]
# search tiers: what each one asks YouTube for
TIER_PARAMS = {
    "fresh": {"order": "date"},                                    # newest, last FRESH_DAYS days
    "top": {"order": "viewCount"},                                 # all-time most watched
    "long": {"videoDuration": "long", "order": "relevance"},       # 20+ minutes (interviews, lectures)
    "mid": {"videoDuration": "medium", "order": "relevance"},      # 4-20 minutes
}
TIERS = {"Goggins": ["fresh", "top", "long"], "Longevity": ["fresh", "top", "long"],
         "Russian": ["top", "long", "mid"], "Cool Stuff": ["fresh", "top", "long", "mid"]}

CHANNEL_DEEP_PAGES = 6              # first time a channel is read: up to 6 x 50 = 300 videos
FRESH_DAYS = 30
NEW_DAYS = 14                       # uploads this recent are never hidden by the "Top picks" filter
MIN_SECONDS = 240                   # skip anything under 4 minutes (kills Shorts)
MAX_QUEUE = 4000                    # unjudged candidates kept for the next runs

# --- Gemini (free tier is limited, so every stage has a hard cap on requests per run) ---
VIDEO_JUDGE_CALLS = 20              # new videos, 50 per request
PLAYLIST_JUDGE_CALLS = 3
RATE_OLD_CALLS = 8                  # gives already-kept videos a quality rating, 50 per request
PAPER_CALLS = 6
SUMMARY_CALLS = 30                  # one video per request
VIDEO_BATCH = 50
PLAYLIST_BATCH = 30
PAPER_BATCH = 15
PAPER_CATS = ("Math", "AI", "Physics", "Quant Finance", "Proofs")   # the only labels the site understands
PAUSE_BETWEEN_CALLS = 8             # seconds (the free tier allows roughly 5-15 requests a minute)

# --- Summaries ---
MAX_TRANSCRIPT_CHARS = 90000        # longer transcripts are thinned out evenly (keeps the whole timeline)
VIDEO_FALLBACK_MAX_SECONDS = 45 * 60   # Gemini may "watch" videos up to this long if no transcript is available
VIDEO_SECONDS_PER_RUN = 3 * 3600       # total video minutes Gemini may watch per run (free tier ~8h/day)
SUMMARY_MAX_FAILS = 3

# --- Ranking ---
PICK_FRACTION = {"Goggins": 0.30, "Longevity": 0.35, "Russian": 0.60, "Cool Stuff": 0.45}   # share of each section marked "top pick"
MIN_PICK_SCORE = 40
LENGTH_FIT_MIN = {"Goggins": (10, 150), "Longevity": (15, 180), "Russian": (6, 60), "Cool Stuff": (6, 90)}   # minutes with full marks
WEIGHTS = {"eng": 0.25, "pop": 0.20, "qual": 0.30, "trust": 0.15, "fit": 0.10}
BAD_KINDS = {"compilation", "reaction", "clickbait", "ad"}
# Channel trust 0..1. Matched as a lowercase substring of the channel name. Unknown channels get 0.5.
TRUSTED = {
    "david goggins": 1.0, "andrew huberman": 1.0, "huberman lab": 1.0, "peter attia": 1.0, "foundmyfitness": 0.95,
    "lex fridman": 0.95, "powerfuljre": 0.9, "joe rogan experience": 0.9, "rich roll": 0.9,
    "chris williamson": 0.85, "modern wisdom": 0.85, "the diary of a ceo": 0.85, "tom bilyeu": 0.8,
    "impact theory": 0.8, "ted": 0.75, "siim land": 0.7, "bryan johnson": 0.7, "david sinclair": 0.9,
    "real russian club": 0.9, "russianpod101": 0.8, "easy russian": 0.85, "russian level 1": 0.8,
    "be fluent in russian": 0.8, "russian with": 0.8, "comprehensible russian": 0.9, "boost your russian": 0.8,
    "learn russian": 0.7, "russian progress": 0.8,
    "numberphile": 1.0, "computerphile": 1.0, "veritasium": 1.0, "3blue1brown": 1.0,
    "stand-up maths": 1.0, "matt parker": 1.0, "minutephysics": 0.95, "pbs infinite series": 0.95,
    "fermilab": 1.0, "smarter every day": 0.95, "steve mould": 0.95, "practical engineering": 0.95,
    "ben eater": 1.0, "sebastian lague": 0.95, "welch labs": 0.95, "royal institution": 0.95,
}
LOW_TRUST_HINTS = ["motivat", "inspir", "mindset", "clips", "compilation", "shorts", "status", "quotes", "viral"]

# --- Research: open-access math, AI, physics and quant finance papers from arXiv (free, no key) ---
PAPER_KEEP_DAYS = 120
FIRST_RUN_MULTIPLIER = 4
ARXIV_PAUSE = 3
PAPER_FEEDS = {
    "Math": ("cat:math.NT OR cat:math.CO OR cat:math.LO OR cat:math.PR OR cat:math.AG OR cat:math.GR "
             "OR cat:math.CA OR cat:math.DS OR cat:math.AT OR cat:math.RT", 5, 400),
    "AI": ("cat:cs.AI OR cat:cs.LG OR cat:cs.CL OR cat:stat.ML", 3, 300),
    "Physics": ("cat:hep-th OR cat:hep-ph OR cat:hep-ex OR cat:gr-qc OR cat:quant-ph OR cat:math-ph OR cat:nucl-th "
                "OR cat:cond-mat.str-el OR cat:cond-mat.supr-con OR cat:cond-mat.mes-hall OR cat:astro-ph.CO "
                "OR cat:astro-ph.HE OR cat:astro-ph.GA", 3, 300),
    "Quant Finance": ("cat:q-fin.RM OR cat:q-fin.PM OR cat:q-fin.MF OR cat:q-fin.CP OR cat:q-fin.ST "
                      "OR cat:q-fin.TR OR cat:q-fin.PR", 10, 300),
    "Proofs": ('(cat:cs.LO OR cat:cs.AI OR cat:cs.LG OR cat:cs.CL OR cat:math.LO) AND '
               '(abs:"theorem proving" OR abs:"formal proof" OR abs:Lean OR abs:autoformalization '
               'OR abs:"proof assistant" OR abs:"formal verification" OR abs:"mathematical reasoning")', 14, 100),
}
GOOD_VENUES = ["NeurIPS", "ICML", "ICLR", "ACL", "EMNLP", "NAACL", "CVPR", "AAAI", "COLT", "STOC", "FOCS",
               "SODA", "LICS", "ITP", "CAV", "JACM", "Annals of Mathematics", "Inventiones", "Acta Mathematica",
               "Journal of the American Mathematical Society", "Duke Mathematical Journal",
               "Forum of Mathematics", "Compositio", "Advances in Mathematics",
               "Mathematical Finance", "Quantitative Finance", "Finance and Stochastics", "Journal of Finance",
               "Review of Financial Studies", "Journal of Financial Economics", "SIAM Journal on Financial Mathematics",
               "Journal of Econometrics", "Journal of Banking & Finance"]
SIGNALS = {"we prove": 3, "we resolve": 4, "we settle": 4, "we disprove": 4, "counterexample": 3,
           "conjecture": 2, "open problem": 3, "new proof": 3, "first proof": 4, "theorem": 2,
           "formalized": 3, "formalised": 3, "lean 4": 3, "machine-checked": 3, "state-of-the-art": 1,
           "scaling law": 2, "mathematical reasoning": 2, "we show that": 1,
           "first observation": 4, "we observe": 1, "experimental": 1, "quantum advantage": 3, "gravitational wave": 2,
           "dark matter": 1, "black hole": 1, "superconduct": 2, "we derive": 2, "exact solution": 3,
           "no-arbitrage": 3, "risk measure": 2, "expected shortfall": 2, "value-at-risk": 2, "tail risk": 2,
           "stochastic volatility": 2, "option pricing": 2, "portfolio optimization": 2, "market microstructure": 2,
           "out-of-sample": 2, "optimal execution": 2, "hedging": 1, "calibration": 1}

LEVELS = ("A1", "A2", "B1", "B2", "C1")
VIDEO_KINDS = ("interview", "talk", "lecture", "explainer", "documentary", "workout", "course", "lesson",
               "compilation", "reaction", "clickbait", "ad", "other")

TODAY = datetime.date.today()
STATE = {"yt_blocked": False, "transcripts_blocked": False, "video_api_broken": False, "video_seconds": 0}
GEM = {"calls": 0, "dead": False}
DESCRIPTIONS = {}            # video id -> description, filled during this run only (never saved)


class QuotaExceeded(Exception):
    pass


# ---------------------------------------------------------------
# 2. FILES (everything is written atomically, so a crash can't leave a half-written file)
# ---------------------------------------------------------------

def read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path, data, pretty=False):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        if pretty:
            json.dump(data, f, ensure_ascii=False, indent=2)
        else:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


def find_out_dir():
    for needed in ("videos.json", "index.html"):
        for d in ("site", "docs", "."):
            if os.path.exists(os.path.join(d, needed)):
                return d
    return "site"


def pacific_day():
    """YouTube's quota resets at midnight Pacific time."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.datetime.now(ZoneInfo("America/Los_Angeles")).date().isoformat()
    except Exception:
        return (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=8)).date().isoformat()


class Library:
    """Everything the site remembers. Loaded first, merged with new finds, saved at the end."""

    def __init__(self, out_dir):
        self.dir = out_dir
        p = self.path
        labels = read_json(p("labels.json"), None)
        if labels is None:
            labels = read_json("labels.json", {})          # older versions kept it in the repo root
        self.labels = labels if isinstance(labels, dict) else {}
        self.channels = read_json(p("channels.json"), {})
        legacy = self.labels.pop("_channels", None)
        if isinstance(legacy, dict):
            for h, info in legacy.items():
                self.channels.setdefault(h, {"uploads": info.get("uploads"), "backfilled": bool(info.get("deep"))})
        self.queue = self.labels.setdefault("_queue", {})
        self.videos = {v["id"]: v for v in read_json(p("videos.json"), []) if isinstance(v, dict) and "id" in v}
        self.playlists = {x["id"]: x for x in read_json(p("playlists.json"), []) if isinstance(x, dict) and "id" in x}
        self.summaries = read_json(p("summaries.json"), {})
        self.search_playlists = []                      # playlist search results found during this run
        self.papers = {x["id"]: x for x in read_json(p("papers.json"), []) if isinstance(x, dict) and "id" in x}
        self.quota = read_json(p("quota.json"), {})
        day = pacific_day()
        if self.quota.get("day") != day:
            self.quota = {"day": day, "used": 0, "done": []}
        self.quota.setdefault("used", 0)
        self.quota.setdefault("done", [])

    def path(self, name):
        return os.path.join(self.dir, name)

    def save_state(self):
        os.makedirs(self.dir, exist_ok=True)
        write_json(self.path("labels.json"), self.labels, pretty=True)
        write_json(self.path("channels.json"), self.channels, pretty=True)
        write_json(self.path("quota.json"), self.quota, pretty=True)


# ---------------------------------------------------------------
# 3. YOUTUBE HELPERS
# ---------------------------------------------------------------

def is_quota_error(err):
    reasons = [e.get("reason", "") for e in err.get("errors", [])]
    return (any(r in ("quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded") for r in reasons)
            or "quota" in (err.get("message") or "").lower())


def yt_get(lib, endpoint, params, cost=1):
    """Call YouTube. Raises QuotaExceeded when today's free limit is used up."""
    try:
        data = requests.get(f"https://www.googleapis.com/youtube/v3/{endpoint}",
                            params=dict(params, key=YOUTUBE_KEY), timeout=30).json()
    except (requests.RequestException, ValueError) as e:
        print("  YouTube request failed:", e)
        return None
    lib.quota["used"] += cost
    err = data.get("error")
    if err:
        if is_quota_error(err):
            raise QuotaExceeded(err.get("message"))
        print("  YouTube said:", err.get("message"))
        return None
    return data


def resolve_channel(lib, handle):
    """@handle -> that channel's 'uploads' playlist (cached in channels.json, so it costs nothing next time)."""
    info = lib.channels.get(handle)
    if info and info.get("uploads"):
        return info
    if info and info.get("missing") and info["missing"] > (TODAY - datetime.timedelta(days=14)).isoformat():
        return None                                         # looked it up recently and it wasn't there
    data = yt_get(lib, "channels", {"part": "contentDetails,snippet", "forHandle": handle})
    items = (data or {}).get("items", [])
    if not items:
        print(f"  Couldn't find channel {handle}. Check the spelling in pipeline.py.")
        if data is not None:
            lib.channels[handle] = {"missing": TODAY.isoformat()}
        return None
    lib.channels[handle] = {"uploads": items[0]["contentDetails"]["relatedPlaylists"]["uploads"], "backfilled": False}
    return lib.channels[handle]


def channel_video_ids(lib, info):
    """Newest uploads of a channel. Deep scan the first time, just the newest 50 after that."""
    pages = 1 if info.get("backfilled") else CHANNEL_DEEP_PAGES
    ids, token = [], None
    for _ in range(pages):
        params = {"part": "contentDetails", "playlistId": info["uploads"], "maxResults": 50}
        if token:
            params["pageToken"] = token
        data = yt_get(lib, "playlistItems", params)
        if not data:
            break
        ids += [x["contentDetails"]["videoId"] for x in data.get("items", [])]
        token = data.get("nextPageToken")
        if not token:
            break
    info["backfilled"] = True
    return ids


def search_youtube(lib, query, tier, kind="video"):
    """One search = 100 units. Returns a list of search items."""
    params = {"part": "snippet", "q": query, "type": kind, "maxResults": 50}
    if kind == "video":
        params.update(TIER_PARAMS[tier])
        if tier == "fresh":
            cutoff = TODAY - datetime.timedelta(days=FRESH_DAYS)
            params["publishedAfter"] = cutoff.isoformat() + "T00:00:00Z"
    data = yt_get(lib, "search", params, cost=100)
    return (data or {}).get("items", [])


def get_details(lib, video_ids):
    """Title, channel, length, views, likes and description for each video (50 per request, 1 unit each)."""
    details, answered = [], True
    for start in range(0, len(video_ids), 50):
        data = yt_get(lib, "videos", {"part": "snippet,contentDetails,statistics,status",
                                      "id": ",".join(video_ids[start:start + 50])})
        if data is None:
            answered = False
            continue
        details.extend(data.get("items", []))
    return details, answered


def duration_to_seconds(iso):
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso or "")
    if not m:
        return 0
    hours, minutes, seconds = (int(x or 0) for x in m.groups())
    return hours * 3600 + minutes * 60 + seconds


BLOCK_REGIONS = {"IN", "GB", "US"}   # regions your viewers are in; a video blocked there cannot play


def playable(item):
    """True only if the video can really play inside an embedded player.
    These are the videos that make YouTube throw the 'Sign in' / 'Video unavailable' window:
    embedding switched off, age-restricted, private/unprocessed, or region-blocked."""
    st = item.get("status") or {}
    cd = item.get("contentDetails") or {}
    if st.get("embeddable") is False:
        return False
    if st.get("privacyStatus") not in (None, "public", "unlisted"):
        return False
    if st.get("uploadStatus") not in (None, "processed"):
        return False
    if (cd.get("contentRating") or {}).get("ytRating") == "ytAgeRestricted":
        return False
    rr = cd.get("regionRestriction") or {}
    if BLOCK_REGIONS & set(rr.get("blocked") or []):
        return False
    allowed = rr.get("allowed")
    if allowed is not None and not (BLOCK_REGIONS <= set(allowed)):
        return False
    return True


def meta_from_item(item):
    sn = item["snippet"]
    st = item.get("statistics") or {}
    likes = st.get("likeCount")
    return {"title": sn["title"], "channel": sn["channelTitle"],
            "seconds": duration_to_seconds((item.get("contentDetails") or {}).get("duration")),
            "views": int(st.get("viewCount", 0) or 0),
            "likes": int(likes) if likes is not None else None,
            "published": sn["publishedAt"][:10]}


LEARN_WORDS = ["learn", "lesson", "beginner", "grammar", "vocabulary", "speak", "listening", "pronunciation",
               "alphabet", "course", "a1", "a2", "b1", "b2", "c1", "урок", "изуч", "comprehensible", "phrases"]


def guess_category(item, searched_for):
    """Quick keyword check for SEARCH results. Returns a section name, or None (= junk)."""
    title = item["snippet"]["title"].lower()
    channel = item["snippet"]["channelTitle"].lower()
    text = title + " " + (item["snippet"].get("description") or "").lower()
    if "goggins" in title or "goggins" in channel:
        return "Goggins"
    if searched_for == "Longevity" and any(w in text for w in
                                           ["longevity", "aging", "ageing", "healthspan", "lifespan",
                                            "live longer", "anti-aging", "vo2 max", "blue zone"]):
        return "Longevity"
    if searched_for == "Cool Stuff":
        return "Cool Stuff"                          # Gemini decides whether it is good enough
    if searched_for == "Russian" and ("russian" in text or "русск" in text) and any(w in text for w in LEARN_WORDS):
        return "Russian"
    return None


def rotate(xs, n):
    if not xs:
        return xs
    n %= len(xs)
    return xs[n:] + xs[:n]


def build_search_plan(done):
    """Round-robin over the sections so none gets starved. Newest-first searches only for the first two
    queries of a section (they find today's new videos); the rest are 'top' and 'long' for the all-time best."""
    shift = TODAY.toordinal()
    per_cat = {}
    for cat in CATS:
        qs = rotate(SEARCHES[cat], shift)
        items = []
        for i, q in enumerate(qs):
            for tier in TIERS[cat]:
                if tier == "fresh" and i >= 2:
                    continue
                items.append((cat, q, tier, "video"))
        per_cat[cat] = items
    plan = []
    for i in range(max(len(v) for v in per_cat.values())):
        for cat in CATS:
            if i < len(per_cat[cat]):
                plan.append(per_cat[cat][i])
    pq = rotate(PLAYLIST_SEARCHES, shift)[0]
    plan.insert(min(3, len(plan)), ("Russian", pq, "playlist", "playlist"))
    return [p for p in plan if "|".join(p[:3]) not in done]


# ---------------------------------------------------------------
# 4. QUALITY SCORE  (pure functions, easy to test: python pipeline.py --rebuild)
# ---------------------------------------------------------------

def channel_trust(name):
    n = (name or "").lower()
    for hint in LOW_TRUST_HINTS:
        if hint in n:
            return 0.3
    best = None
    for key, val in TRUSTED.items():
        if key in n:
            best = val if best is None else max(best, val)
    return 0.5 if best is None else best


def guess_kind(title, channel, seconds):
    """Cheap stand-in until Gemini has rated a video."""
    t = (title + " " + channel).lower()
    if any(w in t for w in ("compilation", "best of", "best moments", "motivational", "motivation", "clips",
                            "whatsapp status")) and seconds < 1800:
        return "compilation"
    if "reaction" in t or "reacts" in t or "reacting" in t:
        return "reaction"
    return "other"


def percentile_ranks(values):
    """0..1 rank of each number among the others. None stays None."""
    known = sorted(v for v in values if v is not None)
    n = len(known)
    out = []
    for v in values:
        if v is None:
            out.append(None)
        elif n < 5:
            out.append(0.5)
        else:
            out.append((bisect.bisect_left(known, v) + bisect.bisect_right(known, v)) / 2 / n)
    return out


def length_fit(cat, seconds):
    lo, hi = LENGTH_FIT_MIN.get(cat, (5, 180))
    m = seconds / 60
    if m < lo:
        return max(0.0, m / lo)
    if m > hi:
        return max(0.4, hi / m)
    return 1.0


def score_videos(videos):
    """Adds 'score' (0-100) and 'pick' (True = top pick) to every video dict, section by section."""
    by_cat = {}
    for v in videos:
        by_cat.setdefault(v["category"], []).append(v)
    for cat, items in by_cat.items():
        rates_known = [v["likes"] / v["views"] for v in items if v.get("likes") is not None and v.get("views")]
        prior = statistics.median(rates_known) if rates_known else 0.025
        weight = 3000                                            # "pretend" 3000 views at the typical rate
        rates = [((v["likes"] + prior * weight) / (v["views"] + weight)) if v.get("likes") is not None else None
                 for v in items]
        reach = [math.log10(v.get("views", 0) + 1) for v in items]
        today = datetime.date.today()

        def age_days(v):
            try:
                return max(30, (today - datetime.date.fromisoformat(v["published"])).days)
            except (KeyError, ValueError):
                return 365
        momentum = [math.log10(v.get("views", 0) / age_days(v) + 1) for v in items]
        r_eng, r_reach, r_mom = percentile_ranks(rates), percentile_ranks(reach), percentile_ranks(momentum)

        for v, e, rc, mo in zip(items, r_eng, r_reach, r_mom):
            eng = 0.5 if e is None else e
            pop = 0.6 * rc + 0.4 * mo
            q = v.get("q")
            qual = 0.5 if not q else (q - 1) / 4
            trust = channel_trust(v.get("channel"))
            fit = length_fit(cat, v.get("seconds", 0))
            raw = (WEIGHTS["eng"] * eng + WEIGHTS["pop"] * pop + WEIGHTS["qual"] * qual
                   + WEIGHTS["trust"] * trust + WEIGHTS["fit"] * fit)
            kind = v.get("kind") or guess_kind(v.get("title", ""), v.get("channel", ""), v.get("seconds", 0))
            if kind in BAD_KINDS:
                raw *= 0.65
            if q and q <= 2:
                raw *= 0.7
            v["score"] = round(100 * raw, 1)

        ranked = sorted((v["score"] for v in items), reverse=True)
        keep_n = max(1, int(round(len(ranked) * PICK_FRACTION.get(cat, 0.35))))
        cutoff = max(MIN_PICK_SCORE, ranked[min(keep_n, len(ranked)) - 1])
        fresh_cut = (today - datetime.timedelta(days=NEW_DAYS)).isoformat()
        for v in items:
            kind = v.get("kind") or guess_kind(v.get("title", ""), v.get("channel", ""), v.get("seconds", 0))
            good = kind not in BAD_KINDS and (not v.get("q") or v["q"] >= 3)
            # a brand-new upload has almost no views, so views-based scores bury it; keep decent ones visible
            is_new = v.get("published", "") >= fresh_cut
            v["pick"] = bool(good and (v["score"] >= cutoff or is_new))


# ---------------------------------------------------------------
# 5. CHAPTERS (free: read from the video description)
# ---------------------------------------------------------------

TS_LINE = re.compile(r"^\s*[\(\[]?(?:(\d{1,2}):)?(\d{1,2}):(\d{2})[\)\]]?\s*[-–—:|.)]*\s*(.{2,120}?)\s*$")


def parse_chapters(description):
    out = []
    for line in (description or "").splitlines():
        m = TS_LINE.match(line)
        if m:
            h, mi, s, title = m.groups()
            out.append([int(h or 0) * 3600 + int(mi) * 60 + int(s), title.strip()])
    if len(out) < 3 or out[0][0] > 30:
        return []
    times = [t for t, _ in out]
    if any(b <= a for a, b in zip(times, times[1:])):
        return []
    return out[:40]


def mmss_to_seconds(text):
    parts = [p for p in re.split(r"[:.]", str(text).strip()) if p.isdigit()]
    if not parts or len(parts) > 3:
        return None
    n = 0
    for p in parts:
        n = n * 60 + int(p)
    return n


# ---------------------------------------------------------------
# 6. RESEARCH PAPER HELPERS  (arXiv: free, open access, no key)
# ---------------------------------------------------------------

ARXIV_API = "https://export.arxiv.org/api/query"
NS = {"a": "http://www.w3.org/2005/Atom", "x": "http://arxiv.org/schemas/atom"}


def find_venue(comment, journal_ref):
    text = f"{comment} {journal_ref}"
    if not (journal_ref or re.search(r"accepted|to appear|appears|published|oral|spotlight|camera", comment, re.I)):
        return ""
    for v in GOOD_VENUES:
        if re.search(r"\b" + re.escape(v) + r"\b", text):
            return v
    return ""


def paper_score(p):
    text = (p["title"] + " " + p["abstract"]).lower()
    score = 5 if p["venue"] else 0
    score += sum(pts for word, pts in SIGNALS.items() if word in text)
    score += 2 if p["code"] else 0
    return score


def fetch_arxiv(feed, query, days, max_results):
    now = datetime.datetime.now(datetime.timezone.utc)
    start = (now - datetime.timedelta(days=days)).strftime("%Y%m%d0000")
    params = {"search_query": f"({query}) AND submittedDate:[{start} TO {now.strftime('%Y%m%d2359')}]",
              "sortBy": "submittedDate", "sortOrder": "descending", "start": 0, "max_results": max_results}
    content = None
    for _ in range(2):
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
            if "/abs/" not in raw_id:
                continue
            arxiv_id = re.sub(r"v\d+$", "", raw_id.split("/abs/")[1])
            title = " ".join(e.findtext("a:title", "", NS).split())
            abstract = " ".join(e.findtext("a:summary", "", NS).split())
            comment = " ".join(e.findtext("x:comment", "", NS).split())
            journal_ref = " ".join(e.findtext("x:journal_ref", "", NS).split())
            prim = e.find("x:primary_category", NS)
            primary = prim.get("term") if prim is not None else ""
            authors = [" ".join(a.findtext("a:name", "", NS).split()) for a in e.findall("a:author", NS)]
            code = re.search(r"https?://(?:www\.)?(?:github\.com|gitlab\.com)/[^\s,;)\]}>\"']+", comment + " " + abstract)
            venue = find_venue(comment, journal_ref)
            p = {"id": f"arxiv:{arxiv_id}", "feed": feed, "title": title, "abstract": abstract,
                 "primary": primary, "venue": venue, "code": code.group(0).rstrip(".") if code else ""}
            p["score"] = paper_score(p)
            p["meta"] = {"title": title,
                         "authors": ", ".join(authors[:3]) + (" et al." if len(authors) > 3 else ""),
                         "journal": "arXiv " + primary + (f" · {venue}" if venue else ""),
                         "date": e.findtext("a:published", "", NS)[:10],
                         "url": f"https://arxiv.org/abs/{arxiv_id}", "pdf": f"https://arxiv.org/pdf/{arxiv_id}",
                         "code": p["code"]}
            papers.append(p)
        except (AttributeError, IndexError, ValueError):
            continue
    print(f"  {len(papers)} papers fetched.")
    return papers


def rank_for_judging(by_feed):
    lists = [sorted(v, key=lambda p: -p["score"]) for v in by_feed.values()]
    out = []
    for i in range(max((len(l) for l in lists), default=0)):
        for l in lists:
            if i < len(l):
                out.append(l[i])
    return out


# ---------------------------------------------------------------
# 7. GEMINI
# ---------------------------------------------------------------

VIDEO_PROMPT = """You are a strict curator for a personal library of exceptional videos. There are exactly four sections:
- "Goggins": David Goggins himself is the main subject.
- "Longevity": evidence-based science of healthspan, aging, exercise, sleep and nutrition.
- "Russian": lessons specifically teaching Russian to non-native learners.
- "Cool Stuff": outstanding mathematics, theoretical and experimental science, physics, computer science, engineering, algorithms, visual explanations and genuinely educational technical demonstrations. Prefer channels such as Numberphile, Computerphile, Veritasium, 3Blue1Brown, Stand-up Maths, Fermilab, Ben Eater, Practical Engineering, SmarterEveryDay, Steve Mould and the Royal Institution.

For "Cool Stuff", KEEP only a real educational video with a clear concept, derivation, experiment, demonstration, build, proof, or technical explanation. REJECT podcast episodes, interviews whose main content is conversation, reaction videos, generic commentary, news roundups, paper-reading/research-paper discussion, AI-generated slop, low-effort listicles, motivational content, shorts, compilations, and vague speculation. A video merely mentioning a scientific paper is not enough. Prefer evergreen teaching and original demonstrations over chatter. Be very selective: if it is not genuinely educational and technically substantive, reject it.

Return for each video: keep, cat, topic (2-4 words), q (quality 1-5), kind. Kinds: interview, talk, lecture, explainer, documentary, workout, course, lesson, demonstration, build, experiment, compilation, reaction, clickbait, ad, other. For Russian videos also give level A1, A2, B1, B2, C1 or Mixed. Return ONLY a JSON array, one object per video, like [{"i":0,"keep":true,"cat":"Cool Stuff","topic":"visual calculus","q":5,"kind":"explainer"},{"i":1,"keep":false}]. "cat" must be exactly "Goggins", "Longevity", "Russian" or "Cool Stuff". Videos: """


RATE_PROMPT = """You are rating videos already on a personal video site with four sections: Goggins, Longevity, Russian and Cool Stuff (excellent maths, science, physics, computer science and engineering teaching). Do NOT decide whether to keep them. Only rate each one.
For Cool Stuff, reward clear teaching, original demonstrations, rigorous explanations and substantive technical content. Give low scores to podcasts, paper-discussion chatter, generic commentary, news roundups and low-effort content.
"q" is quality from 1 to 5. Score substance, clarity, originality and educational value. "kind" is one of: interview, talk, lecture, explainer, documentary, workout, course, lesson, demonstration, build, experiment, compilation, reaction, clickbait, ad, other. Return ONLY a JSON array like [{"i":0,"q":5,"kind":"explainer"}]. Videos: """

PLAYLIST_PROMPT = """You are curating the "Russian" section of a personal site for someone learning Russian.
Below are YouTube playlists. KEEP only playlists that are a structured way to LEARN Russian as a foreign language:
a course, a lesson series, a grammar or vocabulary series, graded listening practice, a story series made for learners.
REJECT random collections, content for native speakers, entertainment, music, other languages, and playlists with fewer than 3 videos.
For each kept playlist give:
- "level": A1, A2, B1, B2, C1 (C1 = advanced), or "Mixed" if it spans several levels
- "topic": 2-4 words
- "q": quality 1-5 (5 = a complete, well structured course from a qualified teacher)
Return ONLY a JSON array, one object per playlist, like this:
[{"i": 0, "keep": true, "level": "A1", "topic": "beginner course", "q": 5}, {"i": 1, "keep": false}]

Playlists:
"""

PAPER_PROMPT = """You are curating a research section for a reader who wants HIGH-LEVEL work in mathematics
(proofs, number theory, combinatorics, logic, ...), AI (reasoning, theory, formal theorem proving,
major advances), PHYSICS (fundamental and theoretical physics: quantum, high-energy, gravity, cosmology,
condensed matter; plus experiments with strong, well-supported results) and QUANT FINANCE (market and credit risk
modelling, derivatives pricing, stochastic and rough volatility, portfolio theory, market microstructure and optimal
execution, statistical finance, and rigorous empirical work with honest out-of-sample evidence). All papers below are open-access arXiv preprints, so quality varies a lot.
KEEP only papers that look substantive and significant: new theorems or proofs, resolved or advanced
conjectures, rigorous theory, formalized or machine-checked proofs (Lean etc.), important new methods,
strong results from credible work. REJECT incremental tweaks, narrow applications, hype, vague or
low-information abstracts. For Quant Finance also REJECT backtest-only trading strategies, crypto or stock price
prediction with naive machine learning, and results that only hold in-sample.
For kept papers also write:
- "cat": "Math", "AI", "Physics", "Quant Finance" or "Proofs" ("Proofs" = formal, machine-checked proofs or AI theorem proving)
- "summary": ONE plain-English sentence (max 35 words) using ONLY what the abstract says. No hype,
  and never add claims that are not in the abstract.
- "kind": one of "new theorem", "new proof", "survey", "new method", "theory", "benchmark", "formal proof", "other"
- "level": "advanced" or "expert"
Return ONLY a JSON array, one object per paper, like this:
[{"i": 0, "keep": true, "cat": "Math", "summary": "...", "kind": "new theorem", "level": "expert"}]

Papers:
"""

SUMMARY_PROMPT = """You summarise a YouTube video so that a busy reader can understand it WITHOUT watching.
Use ONLY the material below. Never add facts, numbers or claims that are not in it. If the material is thin, say less.
Write in plain English, no hype, no "In this video" openers.
Return ONLY a JSON object:
{"tldr": "2-3 sentences, max 60 words, the real answer or main argument",
 "points": ["5-8 key takeaways, one sentence each, max 30 words each, most important first"]%s}
%s
Video title: %s
Channel: %s
Length: %s
Source of the material: %s

MATERIAL:
%s
"""
SUMMARY_EXTRA_MOMENTS = ',\n "moments": [["12:35", "short title"]]'
SUMMARY_EXTRA_RUSSIAN = ',\n "vocab": [["слово", "translation"]]'
SUMMARY_NOTE_MOMENTS = ('"moments": up to 8 important moments, each as ["mm:ss", "short title"], taken from the timestamps '
                        'in the transcript, in time order.')
SUMMARY_NOTE_RUSSIAN = ('"vocab": up to 10 Russian words or phrases the lesson actually teaches, each as '
                        '["Russian", "English translation"]. Only include ones that appear in the material.')


def parse_json_loose(text):
    try:
        return json.loads(text)
    except ValueError:
        pass
    for pat in (r"\[.*\]", r"\{.*\}"):
        m = re.search(pat, text, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except ValueError:
                continue
    raise RuntimeError("Gemini's answer was not valid JSON")


def gemini_request(body):
    """Send one request. Returns the parsed JSON answer. Raises RuntimeError with a readable reason."""
    if GEM["dead"]:
        raise RuntimeError("Gemini's free DAILY limit is already used up")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    for _ in range(3):
        try:
            r = requests.post(url, headers={"x-goog-api-key": GEMINI_KEY}, json=body, timeout=180)
        except requests.RequestException as e:
            raise RuntimeError(f"Gemini request failed: {e}")
        try:
            data = r.json()
        except ValueError:
            data = {}
        if r.status_code == 429:
            msg = (data.get("error", {}).get("message") or "").lower()
            if "day" in msg or "daily" in msg:
                GEM["dead"] = True
                raise RuntimeError("Gemini's free DAILY limit is used up. Whatever is not done yet carries over "
                                   "to the next run.")
            print("  Gemini says slow down. Waiting 40 seconds...")
            time.sleep(40)
            continue
        if r.status_code != 200:
            raise RuntimeError(data.get("error", {}).get("message", str(data))[:300])
        try:
            parts = data["candidates"][0]["content"]["parts"]
            text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        except (KeyError, IndexError, TypeError):
            raise RuntimeError("Gemini sent no answer (possibly blocked by a safety filter)")
        GEM["calls"] += 1
        return parse_json_loose(text)
    raise RuntimeError("Gemini kept saying slow down (free limit). The next run will carry on.")


def call_gemini(prompt):
    return gemini_request({"contents": [{"parts": [{"text": prompt}]}],
                           "generationConfig": {"responseMimeType": "application/json", "temperature": 0}})


def as_list(x):
    if not isinstance(x, list):
        raise RuntimeError("Gemini answered with the wrong shape")
    return x


def clean_level(x):
    x = str(x or "").upper().strip()
    if x in ("C2", "C1+", "C"):
        return "C1"
    return x if x in LEVELS else "Mixed"


def clean_kind(x):
    x = str(x or "").lower().strip()
    return x if x in VIDEO_KINDS else "other"


def clean_q(x):
    try:
        return min(5, max(1, int(round(float(x)))))
    except (TypeError, ValueError):
        return None


def judge_videos(batch):
    lines = [f"[{i}] {v['title']} | channel: {v['channel']} | {v['description'][:150].replace(chr(10), ' ')}"
             for i, v in enumerate(batch)]
    verdicts = {x.get("i"): x for x in as_list(call_gemini(VIDEO_PROMPT + "\n".join(lines))) if isinstance(x, dict)}
    out = []
    for i in range(len(batch)):
        x = verdicts.get(i)
        if x is None:
            out.append(None)                         # no verdict: ask again next run, don't reject for good
        elif x.get("keep") and x.get("cat") in CATS:
            lab = {"keep": True, "cat": x["cat"], "topic": str(x.get("topic", ""))[:60],
                   "q": clean_q(x.get("q")), "kind": clean_kind(x.get("kind"))}
            if x["cat"] == "Russian":
                lab["level"] = clean_level(x.get("level"))
            out.append(lab)
        else:
            out.append({"keep": False})
    return out


def rate_videos(batch):
    lines = [f"[{i}] {v['title']} | channel: {v['channel']} | topic: {v.get('topic', '')} | {v['category']}"
             for i, v in enumerate(batch)]
    verdicts = {x.get("i"): x for x in as_list(call_gemini(RATE_PROMPT + "\n".join(lines))) if isinstance(x, dict)}
    return [({"q": clean_q(verdicts[i].get("q")), "kind": clean_kind(verdicts[i].get("kind"))}
             if i in verdicts and clean_q(verdicts[i].get("q")) else None) for i in range(len(batch))]


def judge_playlists(batch):
    lines = [f"[{i}] {p['title']} | channel: {p['channel']} | {p['count']} videos | "
             f"{p.get('description', '')[:200].replace(chr(10), ' ')}" for i, p in enumerate(batch)]
    verdicts = {x.get("i"): x for x in as_list(call_gemini(PLAYLIST_PROMPT + "\n".join(lines))) if isinstance(x, dict)}
    out = []
    for i in range(len(batch)):
        x = verdicts.get(i)
        if x is None:
            out.append(None)
        elif x.get("keep"):
            out.append({"keep": True, "cat": "Russian", "level": clean_level(x.get("level")),
                        "topic": str(x.get("topic", ""))[:60], "q": clean_q(x.get("q"))})
        else:
            out.append({"keep": False})
    return out


def judge_papers(batch):
    lines = [f"[{i}] ({p['feed']}) {p['title']} | {p['primary']} | {p['abstract'][:600]}" for i, p in enumerate(batch)]
    verdicts = {x.get("i"): x for x in as_list(call_gemini(PAPER_PROMPT + "\n".join(lines))) if isinstance(x, dict)}
    out = []
    for i in range(len(batch)):
        x = verdicts.get(i)
        if x and x.get("keep") and x.get("cat") in PAPER_CATS and x.get("summary"):
            out.append({"keep": True, "cat": x["cat"], "summary": x["summary"],
                        "kind": x.get("kind", "other"), "level": x.get("level", "advanced")})
        elif x is None:
            out.append(None)
        else:
            out.append({"keep": False})
    return out


def run_batches(items, judge, batch_size, max_calls, apply, label):
    """Send items to Gemini in batches (at most max_calls requests). apply(item, result) stores each answer.
    Returns the number of items that were handled. Stops quietly if Gemini has a problem."""
    done = 0
    failures = 0
    for n, start in enumerate(range(0, len(items), batch_size)):
        if n >= max_calls or GEM["dead"]:
            print(f"  {len(items) - start} more {label} will be handled in a later run.")
            break
        batch = items[start:start + batch_size]
        try:
            results = judge(batch)
        except Exception as e:
            print(f"  Gemini problem on {label} batch {n + 1}: {e}")
            if GEM["dead"]:
                break
            failures += 1
            if failures >= 3:                        # 3 bad batches in a row: stop, try again next run
                break
            time.sleep(PAUSE_BETWEEN_CALLS)
            continue                                 # skip the bad batch instead of blocking every batch behind it
        failures = 0
        for item, res in zip(batch, results):
            if res is not None:
                apply(item, res)
                done += 1
        print(f"  Gemini handled {min(start + batch_size, len(items))} / {len(items)} {label}")
        time.sleep(PAUSE_BETWEEN_CALLS)
    return done


# ---------------------------------------------------------------
# 8. SUMMARIES
# ---------------------------------------------------------------

def fmt_len(seconds):
    h, m = seconds // 3600, (seconds % 3600) // 60
    return f"{h}h {m:02d}m" if h else f"{m} min"


def fetch_transcript(video_id):
    """Returns [(start_seconds, text), ...] or None. YouTube often refuses cloud servers, so this can fail."""
    if STATE["transcripts_blocked"]:
        return None
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except ImportError:
        return None
    langs = ["en", "en-US", "en-GB", "ru"]
    try:
        api = YouTubeTranscriptApi()
        if hasattr(api, "fetch"):                                    # version 1.x
            rows = [(float(s.start), s.text) for s in api.fetch(video_id, languages=langs)]
        else:                                                        # version 0.6
            rows = [(float(d["start"]), d["text"]) for d in YouTubeTranscriptApi.get_transcript(video_id, languages=langs)]
    except Exception as e:
        name = type(e).__name__
        if "Block" in name or "IpBlocked" in name or "RequestBlocked" in name:
            STATE["transcripts_blocked"] = True
            print("  YouTube blocks transcript requests from this server. Using other sources from now on.")
        else:
            print(f"  No transcript for {video_id} ({name})")
        return None
    return rows or None


def transcript_text(rows):
    """Merge into ~30 second blocks with a timestamp, thin out evenly if it is too long."""
    blocks, cur_t, cur = [], 0, []
    for t, text in rows:
        if not cur:
            cur_t = int(t)
        cur.append(" ".join(text.split()))
        if t - cur_t >= 30:
            blocks.append((cur_t, " ".join(cur)))
            cur = []
    if cur:
        blocks.append((cur_t, " ".join(cur)))
    total = sum(len(b[1]) + 8 for b in blocks)
    if total > MAX_TRANSCRIPT_CHARS:
        stride = math.ceil(total / MAX_TRANSCRIPT_CHARS)
        blocks = blocks[::stride]
    return "\n".join(f"[{t // 60}:{t % 60:02d}] {s}" for t, s in blocks)


def build_summary_prompt(v, material, source, has_chapters):
    extra, notes = "", []
    if not has_chapters and source == "transcript":
        extra += SUMMARY_EXTRA_MOMENTS
        notes.append(SUMMARY_NOTE_MOMENTS)
    if v["category"] == "Russian":
        extra += SUMMARY_EXTRA_RUSSIAN
        notes.append(SUMMARY_NOTE_RUSSIAN)
    src = {"transcript": "the full spoken transcript with [mm:ss] timestamps",
           "video": "the video itself",
           "description": "ONLY the title, the description and the chapter list (no transcript), so stay general"}[source]
    return SUMMARY_PROMPT % (extra, "\n".join(notes), v["title"], v["channel"], fmt_len(v["seconds"]), src, material)


def clean_summary(obj, source, chapters):
    if not isinstance(obj, dict) or not isinstance(obj.get("tldr"), str) or not obj["tldr"].strip():
        raise RuntimeError("summary had the wrong shape")
    points = [str(p).strip() for p in (obj.get("points") or []) if str(p).strip()][:8]
    out = {"tldr": obj["tldr"].strip()[:600], "points": points, "src": source,
           "date": datetime.date.today().isoformat()}
    if chapters:
        out["ch"] = chapters
    elif source == "transcript":                     # only a transcript has real timestamps to trust
        moments = []
        for m in obj.get("moments") or []:
            if isinstance(m, (list, tuple)) and len(m) == 2 and mmss_to_seconds(m[0]) is not None:
                moments.append([mmss_to_seconds(m[0]), str(m[1]).strip()[:120]])
        moments.sort()
        if len(moments) >= 3:
            out["ch"] = moments[:12]
    vocab = [[str(a).strip(), str(b).strip()] for a, b in
             (x for x in (obj.get("vocab") or []) if isinstance(x, (list, tuple)) and len(x) == 2)][:10]
    if vocab:
        out["vocab"] = vocab
    return out


def summarise_one(v, chapters):
    """Try transcript, then Gemini watching the video, then the description. Returns a summary dict or None."""
    head = "Chapters:\n" + "\n".join(f"{t // 60}:{t % 60:02d} {title}" for t, title in chapters) if chapters else ""
    rows = fetch_transcript(v["id"])
    if rows:
        material = (head + "\n\n" if head else "") + transcript_text(rows)
        try:
            return clean_summary(call_gemini_obj(build_summary_prompt(v, material, "transcript", bool(chapters))),
                                 "transcript", chapters)
        except Exception as e:
            print("  Gemini problem (transcript summary):", e)
            if GEM["dead"]:
                return None
    if (not STATE["video_api_broken"] and v["seconds"] <= VIDEO_FALLBACK_MAX_SECONDS
            and STATE["video_seconds"] + v["seconds"] <= VIDEO_SECONDS_PER_RUN):
        try:
            body = {"contents": [{"parts": [
                        {"file_data": {"file_uri": f"https://www.youtube.com/watch?v={v['id']}"}},
                        {"text": build_summary_prompt(v, head or "(see the video)", "video", bool(chapters))}]}],
                    "generationConfig": {"responseMimeType": "application/json", "temperature": 0.2,
                                         "mediaResolution": "MEDIA_RESOLUTION_LOW"}}
            result = clean_summary(gemini_request(body), "video", chapters)
            STATE["video_seconds"] += v["seconds"]
            time.sleep(PAUSE_BETWEEN_CALLS)
            return result
        except Exception as e:
            print("  Gemini could not watch the video:", e)
            if "DAILY" in str(e):
                return None
            if "400" in str(e) or "not supported" in str(e).lower() or "invalid" in str(e).lower():
                STATE["video_api_broken"] = True
    desc = (DESCRIPTIONS.get(v["id"]) or "").strip()
    if len(desc) < 200 and not chapters:
        return None                                  # nothing to summarise from: better no summary than a made-up one
    material = (head + "\n\n" if head else "") + "Description:\n" + desc[:6000]
    try:
        return clean_summary(call_gemini_obj(build_summary_prompt(v, material, "description", bool(chapters))),
                             "description", chapters)
    except Exception as e:
        print("  Gemini problem (description summary):", e)
        return None


def call_gemini_obj(prompt):
    return gemini_request({"contents": [{"parts": [{"text": prompt}]}],
                           "generationConfig": {"responseMimeType": "application/json", "temperature": 0.2}})


def stage_summaries(lib):
    """Summarise the best-ranked videos that have no summary yet (capped per run)."""
    todo = [v for v in lib.videos.values()
            if not (lib.summaries.get(v["id"]) or {}).get("tldr")
            and lib.labels.get(v["id"], {}).get("sum_fail", 0) < SUMMARY_MAX_FAILS]
    new_cut = (TODAY - datetime.timedelta(days=NEW_DAYS)).isoformat()
    # fresh uploads first (that is what you open the site for), then top picks, then by score
    todo.sort(key=lambda v: (v.get("published", "") < new_cut, not v.get("pick"), -(v.get("score") or 0)))
    print(f"{len(todo)} videos have no summary yet; doing up to {SUMMARY_CALLS} (best first).")
    calls_before, made, attempts = GEM["calls"], 0, 0
    for v in todo:
        if GEM["calls"] - calls_before >= SUMMARY_CALLS or GEM["dead"] or attempts >= SUMMARY_CALLS * 4:
            break
        attempts += 1
        calls_now = GEM["calls"]
        entry = lib.summaries.get(v["id"]) or {}
        result = summarise_one(v, entry.get("ch") or [])
        if result:
            lib.summaries[v["id"]] = result
            made += 1
            print(f"  Summary ({result['src']}): {v['title'][:60]}")
        else:
            lab = lib.labels.setdefault(v["id"], {"keep": True})
            lab["sum_fail"] = lab.get("sum_fail", 0) + 1
        write_json(lib.path("summaries.json"), lib.summaries)          # progress is saved after every video
        if GEM["calls"] > calls_now:
            time.sleep(PAUSE_BETWEEN_CALLS)
    print(f"{made} new summaries.")


# ---------------------------------------------------------------
# 9. STAGES
# ---------------------------------------------------------------

def check_keys(lib):
    keys = [YOUTUBE_KEY, GEMINI_KEY]
    if not all(isinstance(k, str) and k.strip() and "PASTE_" not in k for k in keys):
        print("Open keys.py and paste BOTH keys in it first. (In GitHub: check your two Secrets.)")
        print('Each line should look like:  YOUTUBE_KEY = "AIza...your key..."')
        return False
    ok = True
    try:
        r = requests.get("https://www.googleapis.com/youtube/v3/videos",
                         params={"part": "id", "id": "dQw4w9WgXcQ", "key": YOUTUBE_KEY}, timeout=30).json()
        if "error" in r:
            if is_quota_error(r["error"]):
                print("! YouTube's free daily limit is already used up. Skipping YouTube this run.")
                STATE["yt_blocked"] = True
            else:
                print("X YouTube key FAILED:", r["error"].get("message"))
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
    except (requests.RequestException, ValueError) as e:
        print("X Could not reach Google:", e)
        ok = False
    return ok


def stage_discover(lib):
    """Find video ids from channels, the saved queue, and searches. Returns {id: {"cat", "kind"}}."""
    found = {}
    for vid, cat in list(lib.queue.items()):
        found[vid] = {"cat": cat, "kind": "queue"}
    if STATE["yt_blocked"]:
        return found
    try:
        for cat in CATS:
            for handle in CHANNELS[cat]:
                info = resolve_channel(lib, handle)
                if info:
                    ids = channel_video_ids(lib, info)
                    print(f"Channel {handle}: {len(ids)} videos read")
                    for vid in ids:
                        found.setdefault(vid, {"cat": cat, "kind": "channel"})
        used = 0
        for cat, query, tier, kind in build_search_plan(set(lib.quota["done"])):
            if used >= MAX_SEARCHES_PER_RUN or lib.quota["used"] >= SEARCH_UNITS_CAP:
                break
            used += 1
            print(f"Search {used}/{MAX_SEARCHES_PER_RUN}: [{cat}] {query} ({tier})")
            items = search_youtube(lib, query, tier, kind)
            lib.quota["done"].append(f"{cat}|{query}|{tier}")
            if kind == "playlist":
                lib.search_playlists += items
                continue
            for it in items:
                vid = it["id"].get("videoId")
                if vid and vid not in found:
                    found[vid] = {"cat": cat, "kind": "search", "item": it}
            lib.save_state()
    except QuotaExceeded as e:
        print("\n! YouTube's free daily limit ran out:", e)
        print("  Nothing is lost. What we already have is kept, and the next run carries on.")
    return found


def stage_videos(lib, found):
    """Details for everything we know, refresh the library, pick candidates for Gemini."""
    ids = list(dict.fromkeys(list(found) + list(lib.videos)))
    print(f"\n{len(found)} videos found this run, {len(lib.videos)} in the library.")
    try:
        details, answered = get_details(lib, ids)
    except QuotaExceeded:
        return []
    seen = set()
    candidates, odd, unplayable = [], 0, 0
    fresh_cutoff = (TODAY - datetime.timedelta(days=FRESH_DAYS)).isoformat()
    for item in details:
        try:
            vid = item["id"]
            seen.add(vid)
            meta = meta_from_item(item)
            DESCRIPTIONS[vid] = item["snippet"].get("description", "")
            if not playable(item):                                 # would show YouTube's "Sign in" popup
                if vid in lib.videos:
                    print(f"  Removing unplayable-in-embed video {vid}: {meta['title'][:60]}")
                    unplayable += 1
                lib.videos.pop(vid, None)
                lib.summaries.pop(vid, None)
                lib.queue.pop(vid, None)
                lib.labels[vid] = {"keep": False, "why": "not embeddable"}
                continue
            if vid in lib.videos:                                  # already in the site: refresh numbers, chapters
                rec = lib.videos[vid]
                rec.update({k: meta[k] for k in ("title", "channel", "views", "likes") if meta.get(k) is not None})
                chapters = parse_chapters(DESCRIPTIONS[vid])
                if chapters and not (lib.summaries.get(vid) or {}).get("ch"):
                    lib.summaries.setdefault(vid, {})["ch"] = chapters
                lib.labels.get(vid, {}).pop("miss", None)
                continue
            lab = lib.labels.get(vid)
            if lab is not None and not lab.get("keep"):
                lib.queue.pop(vid, None)
                continue
            if meta["seconds"] < MIN_SECONDS or item["snippet"].get("liveBroadcastContent") in ("live", "upcoming"):
                lib.queue.pop(vid, None)
                continue
            src = found.get(vid, {"cat": "Longevity", "kind": "queue"})
            if src["kind"] == "search":
                guess = guess_category(src["item"], src["cat"])
            elif src["kind"] == "queue":
                guess = src["cat"]
            else:
                guess = src["cat"]
            if guess is None:
                lib.queue.pop(vid, None)
                continue
            candidates.append(dict(meta, id=vid, description=DESCRIPTIONS[vid], guess=guess,
                                   new=meta["published"] >= fresh_cutoff))
        except (KeyError, ValueError, TypeError):
            odd += 1
    if odd:
        print(f"  (skipped {odd} videos with missing details)")
    if unplayable:
        print(f"  Removed {unplayable} videos that cannot be embedded (age-restricted, embedding off, or blocked).")
    if answered:                                                   # videos that disappeared (private or deleted)
        for vid in list(lib.videos):
            if vid not in seen:
                lab = lib.labels.setdefault(vid, {"keep": True})
                lab["miss"] = lab.get("miss", 0) + 1
                if lab["miss"] >= 2:
                    print(f"  Dropping unavailable video {vid}")
                    lib.videos.pop(vid, None)
                    lib.summaries.pop(vid, None)
    return candidates


def stage_judge_videos(lib, candidates):
    candidates.sort(key=lambda v: (not v["new"], -channel_trust(v["channel"]), -v["views"]))
    # Mix the sections so Cool Stuff isn't starved by the big Russian/Longevity backlog.
    buckets = {}
    for v in candidates:
        buckets.setdefault(v["guess"], []).append(v)
    pattern = ["Cool Stuff", "Cool Stuff", "Goggins", "Longevity", "Russian"]
    mixed = []
    while any(buckets.values()):
        for c in pattern:
            if buckets.get(c):
                mixed.append(buckets[c].pop(0))
        for c in list(buckets):
            if c not in pattern and buckets[c]:
                mixed.append(buckets[c].pop(0))
    candidates[:] = mixed
    print(f"{len(candidates)} new videos need Gemini's verdict.")

    def apply(v, lab):
        lib.labels[v["id"]] = lab
        lib.queue.pop(v["id"], None)
        if lab.get("keep"):
            rec = {k: v[k] for k in ("id", "title", "channel", "seconds", "views", "likes", "published")
                   if v.get(k) is not None}
            rec.update({k: lab[k] for k in ("topic", "q", "kind", "level") if lab.get(k) is not None})
            rec["category"] = lab["cat"]
            rec["added"] = TODAY.isoformat()
            lib.videos[v["id"]] = rec
        lib.save_state()

    run_batches(candidates, judge_videos, VIDEO_BATCH, VIDEO_JUDGE_CALLS, apply, "videos")
    # whatever was not judged waits in the queue for the next run
    for v in candidates:
        if v["id"] not in lib.labels and len(lib.queue) < MAX_QUEUE:
            lib.queue[v["id"]] = v["guess"]


def stage_rate_old(lib):
    """Videos that were kept before v5 have no quality rating yet. Rate them, a few batches per run."""
    todo = [v for v in lib.videos.values() if not v.get("q")]
    todo.sort(key=lambda v: -v.get("views", 0))
    if not todo:
        return
    print(f"{len(todo)} saved videos still need a quality rating.")

    def apply(v, res):
        v["q"], v["kind"] = res["q"], res["kind"]

    run_batches(todo, rate_videos, VIDEO_BATCH, RATE_OLD_CALLS, apply, "ratings")


def stage_playlists(lib):
    """Russian playlists: from the Russian channels (cheap) and from playlist searches."""
    cands = {}
    try:
        for handle in CHANNELS["Russian"]:
            info = lib.channels.get(handle)
            if not info or not info.get("uploads"):
                continue
            data = yt_get(lib, "playlists", {"part": "snippet,contentDetails", "maxResults": 50,
                                             "channelId": "UC" + info["uploads"][2:]})
            for it in (data or {}).get("items", []):
                cands[it["id"]] = it
        extra = [it["id"]["playlistId"] for it in lib.search_playlists if it["id"].get("playlistId")]
        extra = [i for i in extra if i not in cands]
        for start in range(0, len(extra), 50):
            data = yt_get(lib, "playlists", {"part": "snippet,contentDetails", "id": ",".join(extra[start:start + 50])})
            for it in (data or {}).get("items", []):
                cands[it["id"]] = it
    except QuotaExceeded:
        print("  YouTube's free limit ran out while reading playlists.")
    todo = []
    for pid, it in cands.items():
        try:
            count = int(it["contentDetails"]["itemCount"])
            sn = it["snippet"]
            thumbs = sn.get("thumbnails") or {}
            thumb = (thumbs.get("medium") or thumbs.get("high") or thumbs.get("default") or {}).get("url", "")
            rec = {"id": pid, "title": sn["title"], "channel": sn["channelTitle"], "count": count,
                   "published": sn["publishedAt"][:10], "thumb": thumb}
        except (KeyError, ValueError, TypeError):
            continue
        key = "pl:" + pid
        if pid in lib.playlists:
            lib.playlists[pid].update({"title": rec["title"], "count": count, "thumb": thumb or lib.playlists[pid].get("thumb", "")})
        elif count >= 3 and key not in lib.labels:
            todo.append(dict(rec, description=sn.get("description", "")))
    todo.sort(key=lambda p: (-channel_trust(p["channel"]), -p["count"]))
    print(f"{len(cands)} Russian playlists seen, {len(todo)} new ones need Gemini's verdict.")

    def apply(p, lab):
        lib.labels["pl:" + p["id"]] = lab
        if lab.get("keep"):
            lib.playlists[p["id"]] = {"id": p["id"], "title": p["title"], "channel": p["channel"], "count": p["count"],
                                      "published": p["published"], "thumb": p["thumb"], "category": "Russian",
                                      "level": lab["level"], "topic": lab["topic"], "q": lab["q"]}
        lib.save_state()

    run_batches(todo, judge_playlists, PLAYLIST_BATCH, PLAYLIST_JUDGE_CALLS, apply, "playlists")


def stage_papers(lib):
    print()
    by_feed, seen = {}, set()
    for feed, (query, days, max_results) in PAPER_FEEDS.items():
        # a feed with nothing kept yet (first run, or a feed added later) looks further back
        first_run = not any(p.get("category") == feed for p in lib.papers.values())
        mult = FIRST_RUN_MULTIPLIER if first_run else 1
        print(f"Searching arXiv: {feed}" + (" (backfill)" if first_run else ""))
        got = fetch_arxiv(feed, query, days * mult, min(max_results * mult, 1000))
        by_feed[feed] = []
        for p in got:
            if p["id"] in seen or p["id"] in lib.papers:
                continue
            seen.add(p["id"])
            lab = lib.labels.get(p["id"])
            if lab is None:
                by_feed[feed].append(p)
            elif lab.get("keep") and p["id"] not in lib.papers:
                lib.papers[p["id"]] = paper_record(p, lab)
        time.sleep(ARXIV_PAUSE)
    todo = rank_for_judging(by_feed)
    print(f"{len(todo)} new papers could be judged (best-looking first).")

    def apply(p, lab):
        lib.labels[p["id"]] = lab
        if lab.get("keep"):
            lib.papers[p["id"]] = paper_record(p, lab)
        lib.save_state()

    if todo:
        run_batches(todo, judge_papers, PAPER_BATCH, PAPER_CALLS, apply, "papers")


def paper_record(p, lab):
    m = p["meta"]
    return {"id": p["id"], "title": m["title"], "authors": m["authors"], "journal": m["journal"], "date": m["date"],
            "url": m["url"], "pdf": m["pdf"], "code": m["code"], "category": lab["cat"], "summary": lab["summary"],
            "kind": lab.get("kind", ""), "level": lab.get("level", ""), "added": TODAY.isoformat()}


# ---------------------------------------------------------------
# 10. BUILD THE SITE FILES
# ---------------------------------------------------------------

def build_site(lib):
    os.makedirs(lib.dir, exist_ok=True)
    videos = []
    for rec in lib.videos.values():
        if rec.get("category") not in CATS or "title" not in rec:
            continue
        v = dict(rec)
        summ = lib.summaries.get(v["id"]) or {}
        if summ.get("tldr"):
            v["s"] = 2
        elif summ.get("ch"):
            v["s"] = 1
        else:
            v.pop("s", None)
        videos.append(v)
    score_videos(videos)
    videos.sort(key=lambda v: -v["score"])
    playlists = sorted(lib.playlists.values(), key=lambda p: (LEVELS.index(p["level"]) if p.get("level") in LEVELS else 9,
                                                              -(p.get("q") or 0), -p.get("count", 0)))
    paper_cutoff = (TODAY - datetime.timedelta(days=PAPER_KEEP_DAYS)).isoformat()
    papers = sorted((p for p in lib.papers.values() if p.get("date", "") >= paper_cutoff),
                    key=lambda p: p["date"], reverse=True)[:200]
    # keep only summaries of videos that are still on the site
    live = {v["id"] for v in videos}
    summaries = {k: v for k, v in lib.summaries.items() if k in live and (v.get("tldr") or v.get("ch"))}

    fresh_cutoff = (TODAY - datetime.timedelta(days=FRESH_DAYS)).isoformat()
    new_videos = sum(1 for v in videos if v["published"] >= fresh_cutoff)
    write_json(lib.path("videos.json"), videos)
    write_json(lib.path("playlists.json"), playlists)
    write_json(lib.path("papers.json"), papers)
    write_json(lib.path("summaries.json"), summaries)
    counts = {c: sum(1 for v in videos if v["category"] == c) for c in CATS}
    write_json(lib.path("status.json"), {
        "updated": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "videos": len(videos), "new_videos": new_videos, "papers": len(papers), "playlists": len(playlists),
        "picks": sum(1 for v in videos if v["pick"]), "summaries": sum(1 for v in summaries.values() if v.get("tldr")),
        "counts": counts})
    lib.save_state()
    print(f"\nSite now has {len(videos)} videos ({', '.join(f'{n} {c}' for c, n in counts.items())}), "
          f"{sum(1 for v in videos if v['pick'])} top picks, {len(playlists)} playlists, "
          f"{len(papers)} papers and {sum(1 for v in summaries.values() if v.get('tldr'))} summaries. "
          f"Saved in {lib.dir}/")


# ---------------------------------------------------------------
# 11. MAIN
# ---------------------------------------------------------------

def guarded(name, fn, *args):
    try:
        return fn(*args)
    except QuotaExceeded as e:
        print(f"\n! YouTube's free daily limit ran out during {name}:", e)
    except Exception as e:                                         # one broken stage must never wipe the site
        print(f"\n! {name} failed ({type(e).__name__}: {e}). Continuing with the rest.")
    return None


def main():
    out_dir = find_out_dir()
    lib = Library(out_dir)
    if "--rebuild" in sys.argv:
        print("Rebuild only: rescoring what is already saved (no network).")
        build_site(lib)
        return
    if not check_keys(lib):
        return
    found = guarded("discovery", stage_discover, lib) or {}
    candidates = guarded("video details", stage_videos, lib, found) or []
    guarded("video judging", stage_judge_videos, lib, candidates)
    guarded("papers", stage_papers, lib)                           # before the low-value stages, so Gemini's daily limit can't starve it
    guarded("playlists", stage_playlists, lib)
    guarded("rating", stage_rate_old, lib)
    build_site(lib)                                                # scores are needed to pick what to summarise
    guarded("summaries", stage_summaries, lib)
    build_site(lib)


if __name__ == "__main__":
    main()
