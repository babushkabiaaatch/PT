
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
MAX_FOR_GEMINI = 300       # top-viewed videos sent to Gemini (ALL fresh ones are added on top)
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
MAX_FOR_GEMINI = 300       # top-viewed videos sent to Gemini (ALL fresh ones are added on top)
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
