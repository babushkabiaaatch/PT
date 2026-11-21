#!/usr/bin/env python3
"""
GogginsTube pipeline  (YouTube + multi-source research + Gemini)
---------------------------------------------------------------------
What this does, in plain words:
  1. Checks both of your keys work (keys live in keys.py)
  2. Searches YouTube twice: once for the all-time best videos, once for the NEWEST ones
  3. Searches Europe PMC, PubMed, bioRxiv, medRxiv, arXiv, Crossref and optionally
     Springer Nature for recent longevity and mental-toughness research metadata
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
from itertools import zip_longest
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
GEMINI_MODEL = os.environ.get("GEMINI_MODEL") or "gemini-3.5-flash-lite"

VIDEO_SEARCHES = {
    "Goggins": [
        "David Goggins motivation",
        "David Goggins interview",
        "David Goggins podcast",
        "David Goggins speech",
        "David Goggins Can't Hurt Me",
        "David Goggins workout",
        "David Goggins Joe Rogan",
        "David Goggins Impact Theory",
        "David Goggins Navy SEAL Hell Week",
        "David Goggins Never Finished",
        "David Goggins Rich Roll",
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

PAGES_PER_SEARCH = 1       # (old setting, no longer used)

# ---- YouTube quota plan --------------------------------------------------
# YouTube gives your key 10,000 "units" per day (resets at midnight Pacific time).
#   one search        = 100 units  (expensive: only 100 searches a day!)
#   channel / playlist / video lookups = 1 unit (cheap)
# So we find most videos through CHANNELS (cheap) and only do a few searches per run.
DAILY_QUOTA = 10000
QUOTA_RESERVE = 1500       # never spend the last 1,500 units (keeps room for your own testing)
SEARCHES_PER_RUN = 8       # all-time searches per run (8 x 100 = 800 units). They rotate daily.
BACKFILL_PAGES = 8         # first time we see a channel: read its newest 8 x 50 = 400 videos
DAILY_PAGES = 1            # afterwards: only the newest 50 per channel (1 unit)
MAX_NEW_PER_CATEGORY = 120 # new videos sent to Gemini per category per run (rest waits for tomorrow)

# Channels to read directly.  "all": True = every video counts as a candidate (focused channel).
# "all": False = only videos whose title/description mention a longevity word (general channel).
# If a handle can't be found, the log says so and it is skipped: fix the spelling or delete the line.
CHANNELS = {
    "Goggins": [
        {"handle": "@davidgoggins", "all": True},
    ],
    "Longevity": [
        {"handle": "@PeterAttiaMD", "all": True},
        {"handle": "@FoundMyFitness", "all": True},
        {"handle": "@hubermanlab", "all": False},
        {"handle": "@bryanjohnson", "all": False},
        {"handle": "@NutritionFacts", "all": False},
        {"handle": "@SiimLand", "all": False},
    ],
}

LONGEVITY_RE = re.compile(
    r"longevity|healthspan|lifespan|aging|ageing|vo2|zone 2|sleep|fasting|metabolic|insulin|"
    r"muscle|strength|exercise|nutrition|protein|cholesterol|apob|glucose|alzheimer|dementia|"
    r"supplement|inflammation|mitochondri|autophagy|rapamycin|blue zone|biological age|cardio|"
    r"hormone|testosterone|menopause|gut|microbio|cancer|heart", re.I)
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
    """Check required credentials without spending YouTube search quota."""
    if not YOUTUBE_KEY:
        print("Missing YOUTUBE_API_KEY. Existing videos will be retained; papers can still update.")
    if not GEMINI_KEY:
        print("Missing GEMINI_API_KEY. Existing libraries will be retained; no AI judging can run.")
        return False
    # Do not call YouTube here: a quota-exhausted YouTube API must not block paper discovery.
    return True


# ---------------------------------------------------------------
# 3. YOUTUBE HELPERS  (quota-aware: remembers what it spent today)
# ---------------------------------------------------------------

QUOTA_PATH = os.path.join(OUT_DIR, "quota.json")
CHANNELS_PATH = os.path.join(OUT_DIR, "channels.json")
QUOTA = {"day": "", "used": 0, "done": []}


def pacific_day():
    """YouTube's quota day changes at midnight Pacific time."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.datetime.now(ZoneInfo("America/Los_Angeles")).date().isoformat()
    except Exception:  # Windows without tzdata: close enough
        return (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=8)).date().isoformat()


def load_quota():
    """Load today's spending from quota.json so repeated runs don't burn the quota twice."""
    global QUOTA
    day = pacific_day()
    data = {}
    try:
        with open(QUOTA_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        pass
    if isinstance(data, dict) and data.get("day") == day:
        QUOTA = {"day": day, "used": int(data.get("used", 0)), "done": list(data.get("done", []))}
    else:
        QUOTA = {"day": day, "used": 0, "done": []}
    print(f"YouTube quota today ({day}): about {QUOTA['used']} of {DAILY_QUOTA} units already used.")


def save_quota():
    write_json_atomic(QUOTA_PATH, QUOTA)


def can_spend(units):
    return QUOTA["used"] + units <= DAILY_QUOTA - QUOTA_RESERVE


def youtube_get(endpoint, params, cost):
    """Call the YouTube API. Returns the data, or None if skipped/out of quota.
    Raises RuntimeError for real problems (bad key, API not enabled...)."""
    global YOUTUBE_QUOTA_EXHAUSTED
    if YOUTUBE_QUOTA_EXHAUSTED:
        return None
    if not can_spend(cost):
        print("  Stopping YouTube calls: today's safe quota budget is used up.")
        return None
    response = requests.get(
        f"https://www.googleapis.com/youtube/v3/{endpoint}",
        params={**params, "key": YOUTUBE_KEY}, timeout=45)
    try:
        data = response.json()
    except ValueError:
        raise RuntimeError(f"YouTube returned invalid JSON (HTTP {response.status_code}).")
    if response.status_code != 200 or "error" in data:
        error = data.get("error", {})
        message = error.get("message", response.text[:500])
        reasons = {item.get("reason", "") for item in error.get("errors", [])}
        if reasons & {"quotaExceeded", "dailyLimitExceeded"}:
            YOUTUBE_QUOTA_EXHAUSTED = True
            QUOTA["used"] = DAILY_QUOTA          # remember: nothing more today
            save_quota()
            print("WARNING: YouTube daily quota is used up. It resets at midnight Pacific time.")
            return None
        if response.status_code == 429 or "rateLimitExceeded" in reasons:
            YOUTUBE_QUOTA_EXHAUSTED = True       # too fast right now; stop for this run only
            print("WARNING: YouTube says slow down. Stopping YouTube calls for this run.")
            return None
        raise RuntimeError(f"YouTube {endpoint} failed (HTTP {response.status_code}): {message}")
    QUOTA["used"] += cost
    save_quota()
    return data


def load_json_dict(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def resolve_channel(handle, state):
    """Turn '@handle' into the channel's 'uploads' playlist id (1 unit, then remembered)."""
    entry = state.get(handle)
    if entry and entry.get("uploads"):
        return entry
    data = youtube_get("channels", {"part": "contentDetails", "forHandle": handle}, 1)
    if not data:
        return None
    items = data.get("items") or []
    if not items:
        print(f"  Could not find channel {handle}. Check the spelling in CHANNELS.")
        return None
    entry = {"uploads": items[0]["contentDetails"]["relatedPlaylists"]["uploads"], "backfilled": False}
    state[handle] = entry
    return entry


def channel_video_ids(handle, state):
    """Newest videos of a channel. 1 unit per 50 videos (a search would cost 100)."""
    entry = resolve_channel(handle, state)
    if not entry:
        return []
    pages = DAILY_PAGES if entry.get("backfilled") else BACKFILL_PAGES
    ids, token, complete = [], None, True
    for _ in range(pages):
        params = {"part": "contentDetails", "playlistId": entry["uploads"], "maxResults": 50}
        if token:
            params["pageToken"] = token
        data = youtube_get("playlistItems", params, 1)
        if data is None:
            complete = False
            break
        ids.extend(i["contentDetails"]["videoId"] for i in data.get("items", [])
                   if i.get("contentDetails", {}).get("videoId"))
        token = data.get("nextPageToken")
        if not token:
            break
    if complete:
        entry["backfilled"] = True
    print(f"  {handle}: {len(ids)} videos read")
    return ids


def search_youtube(query, fresh=False, duration=None):
    """One YouTube search = 100 units. Used sparingly, to find videos on OTHER people's channels."""
    params = {"part": "snippet", "q": query, "type": "video", "maxResults": 50}
    if fresh:
        cutoff = TODAY - datetime.timedelta(days=FRESH_DAYS)
        params["order"] = "date"
        params["publishedAfter"] = cutoff.isoformat() + "T00:00:00Z"
    elif duration:
        params["videoDuration"] = duration       # "long" (20+ min) or "medium" (4-20 min): skips Shorts
    data = youtube_get("search", params, 100)
    if not data:
        return []
    return [item["id"]["videoId"] for item in data.get("items", [])
            if item.get("id", {}).get("videoId")]


def pick_searches():
    """Pick today's searches. They rotate, so over a few days every query gets its turn."""
    groups = [[(cat, q) for q in queries] for cat, queries in VIDEO_SEARCHES.items()]
    mixed = [x for row in zip_longest(*groups) for x in row if x]   # alternates Goggins / Longevity
    if not mixed:
        return []
    start = (TODAY.toordinal() * SEARCHES_PER_RUN) % len(mixed)
    return [mixed[(start + i) % len(mixed)] for i in range(min(SEARCHES_PER_RUN, len(mixed)))]


def get_details(video_ids):
    """Fetch title, channel, length and views for each video (50 at a time, 1 unit each)."""
    details = []
    for start in range(0, len(video_ids), 50):
        chunk = video_ids[start:start + 50]
        data = youtube_get("videos", {"part": "snippet,contentDetails,statistics", "id": ",".join(chunk)}, 1)
        if data is None:
            break
        details.extend(data.get("items", []))
    return details


def duration_to_seconds(iso):
    """YouTube gives lengths like 'PT1H2M3S'. Turn that into seconds."""
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso or "")
    if not m:
        return 0
    hours, minutes, seconds = (int(x or 0) for x in m.groups())
    return hours * 3600 + minutes * 60 + seconds


def guess_category(item, info):
    """Decide which section a video could belong to (Gemini makes the final call)."""
    snippet = item.get("snippet", {})
    title = snippet.get("title", "")
    channel = snippet.get("channelTitle", "")
    cats, modes = info.get("cats", set()), info.get("modes", set())

    if "goggins" in title.lower() or "goggins" in channel.lower():
        return "Goggins"
    if "Goggins" in cats and "trusted" in modes:        # e.g. Goggins' own channel
        return "Goggins"
    if "Longevity" in cats:
        if modes & {"trusted", "search"}:                # focused channel, or a longevity search hit
            return "Longevity"
        if LONGEVITY_RE.search(title + " " + snippet.get("description", "")[:500]):
            return "Longevity"
    return None


# ---------------------------------------------------------------
# 4. RESEARCH PAPER HELPERS  (Europe PMC: free, no key needed)
# ---------------------------------------------------------------

def strip_tags(text):
    """Paper titles/abstracts sometimes contain <i>html</i> tags. Remove them."""
    return re.sub(r"<[^>]+>", "", text or "").strip()


def _paper_record(source, source_id, title, authors="", journal="", date="", url="", abstract="", category="Longevity", preprint=False, doi=""):
    title = strip_tags(title)
    if not title:
        return None
    stable = (doi or f"{source}:{source_id}").lower().strip()
    return {
        "id": "paper:" + stable,
        "title": title,
        "authors": strip_tags(authors)[:300],
        "journal": strip_tags(journal) or source,
        "date": str(date or "")[:10],
        "url": url or (f"https://doi.org/{doi}" if doi else ""),
        "preprint": bool(preprint),
        "abstract": strip_tags(abstract)[:5000],
        "guess": category,
        "source": source,
        "doi": doi or "",
    }


def _category_for_text(text):
    value = (text or "").lower()
    toughness_terms = ("mental toughness", "psychological resilience", "grit", "ultra-endurance", "perceived exertion", "stress resilience")
    return "Toughness" if any(term in value for term in toughness_terms) else "Longevity"


def _date_window():
    return ((TODAY - datetime.timedelta(days=NEW_PAPER_DAYS)).isoformat(), TODAY.isoformat())


def fetch_europe_pmc(papers):
    since, until = _date_window()
    for category, terms in PAPER_SEARCHES.items():
        query = f"({terms}) AND (FIRST_PDATE:[{since} TO {until}]) AND (HAS_ABSTRACT:y)"
        try:
            response = requests.get("https://www.ebi.ac.uk/europepmc/webservices/rest/search", params={
                "query": query, "format": "json", "resultType": "core", "pageSize": 100,
            }, timeout=45)
            response.raise_for_status()
            for item in response.json().get("resultList", {}).get("result", []):
                doi = item.get("doi", "")
                source, pid = item.get("source", "EPMC"), item.get("id", "")
                url = f"https://doi.org/{doi}" if doi else f"https://europepmc.org/article/{source}/{pid}"
                rec = _paper_record("Europe PMC", f"{source}:{pid}", item.get("title"), item.get("authorString"),
                    item.get("journalTitle") or "Preprint", item.get("firstPublicationDate") or item.get("pubYear"),
                    url, item.get("abstractText"), category, source == "PPR", doi)
                if rec and rec["abstract"]: papers[rec["id"]] = rec
            print(f"  Europe PMC ({category}) completed")
        except Exception as exc:
            print(f"  Europe PMC ({category}) unavailable: {exc}")


def fetch_pubmed(papers):
    """Fetch PubMed abstracts using NCBI E-utilities; API key is optional."""
    since, until = _date_window()
    for category, terms in PAPER_SEARCHES.items():
        try:
            q = f"({terms}) AND ({since}[Date - Publication] : {until}[Date - Publication])"
            params = {"db":"pubmed", "term":q, "retmode":"json", "retmax":50, "sort":"date"}
            if os.environ.get("NCBI_API_KEY"): params["api_key"] = os.environ["NCBI_API_KEY"]
            search = requests.get("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi", params=params, timeout=40)
            search.raise_for_status()
            ids = search.json().get("esearchresult", {}).get("idlist", [])
            if not ids: continue
            fetch_params = {"db":"pubmed", "id":",".join(ids), "retmode":"xml"}
            if os.environ.get("NCBI_API_KEY"): fetch_params["api_key"] = os.environ["NCBI_API_KEY"]
            result = requests.get("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi", params=fetch_params, timeout=60)
            result.raise_for_status()
            root = ET.fromstring(result.text)
            for article in root.findall('.//PubmedArticle'):
                pmid = article.findtext('.//PMID', default='')
                title = ''.join(article.find('.//ArticleTitle').itertext()) if article.find('.//ArticleTitle') is not None else ''
                abstract = ' '.join(''.join(x.itertext()) for x in article.findall('.//Abstract/AbstractText'))
                authors = []
                for au in article.findall('.//Author')[:5]:
                    name = ' '.join(x for x in [au.findtext('ForeName'), au.findtext('LastName')] if x)
                    if name: authors.append(name)
                journal = article.findtext('.//Journal/Title', default='PubMed')
                pubdate = article.findtext('.//PubDate/Year') or article.findtext('.//PubDate/MedlineDate', default='')
                doi = ''
                for aid in article.findall('.//ArticleId'):
                    if aid.attrib.get('IdType') == 'doi': doi = aid.text or ''; break
                rec = _paper_record('PubMed', pmid, title, ', '.join(authors), journal, pubdate,
                    f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/", abstract, category, False, doi)
                if rec and rec['abstract']: papers[rec['id']] = rec
            print(f"  PubMed ({category}) completed")
            time.sleep(0.4)
        except Exception as exc:
            print(f"  PubMed ({category}) unavailable: {exc}")


def fetch_biorxiv_server(papers, server):
    since, until = _date_window()
    try:
        url = f"https://api.biorxiv.org/details/{server}/{since}/{until}/0/json"
        records = []
        # bioRxiv/medRxiv return paginated batches; fetch a few pages, not just the first 30.
        for cursor in range(0, 150, 30):
            page_url = f"https://api.biorxiv.org/details/{server}/{since}/{until}/{cursor}/json"
            response = requests.get(page_url, timeout=60, headers={"User-Agent":"GogginsTube/1.0 (research metadata aggregator)"})
            response.raise_for_status()
            page = response.json().get("collection", [])
            if not page:
                break
            records.extend(page)
            if len(page) < 30:
                break
            time.sleep(1.0)
        for item in records:
            text = " ".join([item.get("title", ""), item.get("abstract", ""), item.get("category", "")])
            category = _category_for_text(text)
            # Keep only papers relevant to the two site's research themes.
            terms = PAPER_SEARCHES[category].lower().replace('"','')
            haystack = text.lower()
            keywords = [w.strip() for w in terms.replace(' or ', '|').split('|') if len(w.strip()) > 4]
            if not any(w in haystack for w in keywords): continue
            doi = item.get("doi", "")
            rec = _paper_record(server, doi or item.get("title", ""), item.get("title"), item.get("authors"),
                server, item.get("date"), f"https://doi.org/{doi}" if doi else "", item.get("abstract"), category, True, doi)
            if rec and rec['abstract']: papers[rec['id']] = rec
        print(f"  {server} completed ({len(records)} records scanned)")
    except Exception as exc:
        print(f"  {server} unavailable: {exc}")


def fetch_arxiv(papers):
    since, until = _date_window()
    query = ' OR '.join(['all:"biological aging"', 'all:longevity', 'all:healthspan', 'all:"mental resilience"', 'all:"exercise physiology"'])
    try:
        response = requests.get("https://export.arxiv.org/api/query", params={
            "search_query": query, "start": 0, "max_results": 60, "sortBy":"submittedDate", "sortOrder":"descending"
        }, timeout=60, headers={"User-Agent":"GogginsTube/1.0"})
        response.raise_for_status()
        root = ET.fromstring(response.text)
        ns = {"a":"http://www.w3.org/2005/Atom"}
        for entry in root.findall('a:entry', ns):
            published = entry.findtext('a:published', default='', namespaces=ns)[:10]
            if not (since <= published <= until): continue
            title = ' '.join(entry.findtext('a:title', default='', namespaces=ns).split())
            abstract = ' '.join(entry.findtext('a:summary', default='', namespaces=ns).split())
            link = next((x.attrib.get('href','') for x in entry.findall('a:link', ns) if x.attrib.get('rel') == 'alternate'), '')
            aid = entry.findtext('a:id', default='', namespaces=ns).rsplit('/',1)[-1]
            authors = ', '.join(x.findtext('a:name', default='', namespaces=ns) for x in entry.findall('a:author', ns)[:4])
            category = _category_for_text(title + ' ' + abstract)
            rec = _paper_record('arXiv', aid, title, authors, 'arXiv preprint', published, link, abstract, category, True)
            if rec and rec['abstract']: papers[rec['id']] = rec
        print("  arXiv completed")
    except Exception as exc:
        print(f"  arXiv unavailable: {exc}")
    time.sleep(3.1)  # arXiv requests should be spaced conservatively.


def fetch_crossref(papers):
    since, until = _date_window()
    mailto = os.environ.get("CONTACT_EMAIL", "")
    for category, terms in PAPER_SEARCHES.items():
        try:
            phrase = "longevity healthspan ageing" if category == "Longevity" else "mental toughness psychological resilience endurance"
            params = {"query.bibliographic":phrase, "filter":f"from-pub-date:{since},until-pub-date:{until}", "rows":50, "select":"DOI,title,author,container-title,published,abstract,type"}
            if mailto: params["mailto"] = mailto
            response = requests.get("https://api.crossref.org/works", params=params, timeout=45,
                headers={"User-Agent":f"GogginsTube/1.0 (mailto:{mailto})" if mailto else "GogginsTube/1.0"})
            response.raise_for_status()
            for item in response.json().get("message", {}).get("items", []):
                title = (item.get("title") or [""])[0]
                abstract = item.get("abstract", "")
                doi = item.get("DOI", "")
                authors = ', '.join(' '.join(x for x in [a.get('given'),a.get('family')] if x) for a in item.get('author',[])[:4])
                dateparts = (item.get('published') or {}).get('date-parts', [[]])[0]
                pubdate = '-'.join(str(x).zfill(2) if i else str(x) for i,x in enumerate(dateparts)) if dateparts else ''
                journal = (item.get("container-title") or [""])[0] or "Crossref"
                rec = _paper_record('Crossref', doi, title, authors, journal, pubdate, f"https://doi.org/{doi}" if doi else "", abstract, category, False, doi)
                # Crossref often has no abstract; avoid sending empty/metadata-only records to Gemini.
                if rec and rec['abstract']: papers[rec['id']] = rec
            print(f"  Crossref ({category}) completed")
        except Exception as exc:
            print(f"  Crossref ({category}) unavailable: {exc}")


def fetch_springer_nature(papers):
    api_key = os.environ.get("SPRINGER_NATURE_API_KEY", "")
    if not api_key:
        print("  Springer Nature skipped (optional SPRINGER_NATURE_API_KEY not configured).")
        return
    for category, phrase in [("Longevity", "longevity OR healthspan OR ageing"), ("Toughness", '"mental toughness" OR resilience OR endurance')]:
        try:
            response = requests.get("https://api.springernature.com/meta/v2/json", params={
                "q":phrase, "p":50, "api_key":api_key
            }, timeout=45)
            response.raise_for_status()
            for item in response.json().get("records", []):
                doi = item.get("doi", "")
                title = item.get("title", "")
                abstract = item.get("abstract", "")
                pubdate = item.get("publicationDate", "")[:10]
                rec = _paper_record('Springer Nature', doi or title, title, item.get("creators", ""),
                    item.get("publicationName", "Springer Nature"), pubdate,
                    f"https://doi.org/{doi}" if doi else item.get("url", ""), abstract, category, False, doi)
                if rec and rec['abstract']: papers[rec['id']] = rec
            print(f"  Springer Nature ({category}) completed")
        except Exception as exc:
            print(f"  Springer Nature ({category}) unavailable: {exc}")


def fetch_papers():
    """Aggregate recent metadata/abstracts from independent scholarly sources.

    Every source is optional at runtime: a failure in one provider does not block others.
    Full-text articles are not downloaded; the site links to publisher/open versions.
    """
    papers = {}
    providers = [
        ("Europe PMC", lambda: fetch_europe_pmc(papers)),
        ("PubMed", lambda: fetch_pubmed(papers)),
        ("bioRxiv", lambda: fetch_biorxiv_server(papers, "biorxiv")),
        ("medRxiv", lambda: fetch_biorxiv_server(papers, "medrxiv")),
        ("arXiv", lambda: fetch_arxiv(papers)),
        ("Crossref", lambda: fetch_crossref(papers)),
        ("Springer Nature", lambda: fetch_springer_nature(papers)),
    ]
    for name, fetcher in providers:
        try:
            fetcher()
        except Exception as exc:
            print(f"  {name} failed; continuing with other sources: {exc}")
    result = sorted(papers.values(), key=lambda x: x.get("date", ""), reverse=True)
    print(f"  Aggregated {len(result)} unique papers across providers.")
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
        lines.append(f"[{i}] expected category: {v.get('guess', 'unknown')} | {v['title']} | channel: {v['channel']} | {desc}")
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
    """Keep Gemini verdict cache inside site/ so GitHub Actions commits it."""
    path = os.path.join(OUT_DIR, "labels.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    return {}


def save_labels(labels):
    write_json_atomic(os.path.join(OUT_DIR, "labels.json"), labels)


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
    os.makedirs(OUT_DIR, exist_ok=True)
    videos_path = os.path.join(OUT_DIR, "videos.json")
    papers_path = os.path.join(OUT_DIR, "papers.json")
    status_path = os.path.join(OUT_DIR, "status.json")
    existing_videos = load_existing_list(videos_path)
    existing_papers = load_existing_list(papers_path)

    if not GEMINI_KEY:
        print("Missing GEMINI_API_KEY. Cannot judge new items; existing site data will be retained.")
        gemini_available = False
    else:
        gemini_available = True

    # --- Video discovery (cheap channel reading first, a few rotating searches last). ---
    found = {}   # video_id -> {"cats": {...}, "modes": {...}}

    def note(ids, category, mode):
        for video_id in ids:
            info = found.setdefault(video_id, {"cats": set(), "modes": set()})
            info["cats"].add(category)
            info["modes"].add(mode)

    load_quota()
    if not YOUTUBE_KEY:
        print("No YOUTUBE_API_KEY configured. Retaining existing videos and continuing with papers.")
    else:
        channel_state = load_json_dict(CHANNELS_PATH)
        for category, channels in CHANNELS.items():
            for channel in channels:
                print("Reading channel:", channel["handle"])
                try:
                    note(channel_video_ids(channel["handle"], channel_state),
                         category, "trusted" if channel["all"] else "channel")
                except Exception as exc:
                    print(f"  Channel failed ({channel['handle']}): {exc}")
        write_json_atomic(CHANNELS_PATH, channel_state)

        duration = "long" if TODAY.toordinal() % 2 == 0 else "medium"
        todo_searches = [(c, q, False) for c, q in pick_searches()]
        todo_searches += [(c, q, True) for c, q in FRESH_SEARCHES.items()]
        for category, query, fresh in todo_searches:
            key = f"{category}|{query}|{'fresh' if fresh else duration}"
            if key in QUOTA["done"]:
                print("Already searched today, skipping:", query)
                continue
            if YOUTUBE_QUOTA_EXHAUSTED or not can_spend(100):
                print("Out of safe YouTube budget; remaining searches wait for the next run.")
                break
            print("Searching", "recent" if fresh else "videos", ":", query)
            try:
                ids = search_youtube(query, fresh=fresh, duration=duration)
                note(ids, category, "search")
                if ids:
                    QUOTA["done"].append(key)
                    save_quota()
            except Exception as exc:
                print(f"  Search failed ({query}): {exc}")

    newly_kept_videos = []
    candidates = []
    labels = load_labels()
    fresh_cutoff = (TODAY - datetime.timedelta(days=FRESH_DAYS)).isoformat()
    known_ids = {v.get("id") for v in existing_videos}

    # Only look up videos we have not judged before (saves quota and Gemini calls).
    need = [vid for vid in found
            if vid not in labels or (labels[vid].get("keep") and vid not in known_ids)]
    print(f"Found {len(found)} videos in total; {len(need)} are new to us.")

    if need:
        try:
            for item in get_details(need):
                snippet = item.get("snippet") or {}
                content_details = item.get("contentDetails") or {}
                if not snippet or not content_details.get("duration"): continue
                seconds = duration_to_seconds(content_details["duration"])
                guess = guess_category(item, found.get(item.get("id"), {}))
                if seconds < MIN_SECONDS or guess is None: continue
                candidates.append({
                    "id":item["id"], "title":snippet.get("title", ""),
                    "channel":snippet.get("channelTitle", ""), "description":snippet.get("description", ""),
                    "seconds":seconds, "views":int((item.get("statistics") or {}).get("viewCount", 0)),
                    "published":snippet.get("publishedAt", "")[:10], "guess":guess,
                })
            # Already judged earlier + a fair share of new ones per category (so neither section starves).
            chosen = [v for v in candidates if v["id"] in labels]
            if gemini_available:
                for category in ("Goggins", "Longevity"):
                    group = sorted((v for v in candidates if v["id"] not in labels and v["guess"] == category),
                                   key=lambda v: -v["views"])
                    picks = group[:MAX_NEW_PER_CATEGORY]
                    picks += [v for v in group[MAX_NEW_PER_CATEGORY:] if v["published"] >= fresh_cutoff][:40]
                    chosen.extend(picks)
                    waiting = len(group) - len(picks)
                    if waiting > 0:
                        print(f"  {waiting} more {category} videos will be judged on the next run.")
            print(f"Eligible video candidates: {len(candidates)}; sending to review: {len(chosen)}.")
            if chosen:
                if gemini_available and any(v["id"] not in labels for v in chosen):
                    if not judge_all(chosen, labels, judge_videos, VIDEO_BATCH):
                        print("Gemini video review incomplete; the rest will be judged on the next run.")
                for video in chosen:
                    label = labels.get(video["id"])
                    if not label or not label.get("keep"): continue
                    category = label.get("cat")
                    if category not in ("Goggins", "Longevity"):
                        category = video["guess"]
                    newly_kept_videos.append({
                        "id":video["id"], "title":video["title"], "channel":video["channel"],
                        "seconds":video["seconds"], "views":video["views"], "published":video["published"],
                        "category":category, "topic":label.get("topic", ""),
                    })
        except Exception as exc:
            print(f"Video processing failed; existing videos will be retained: {exc}")
    elif YOUTUBE_QUOTA_EXHAUSTED:
        print("YouTube quota exhausted. Skipping video discovery; continuing to research providers.")

    videos = merge_by_id(existing_videos, newly_kept_videos)
    videos.sort(key=lambda video: (-int(video.get("views", 0)), video.get("published", "")))
    write_json_atomic(videos_path, videos)
    goggins_count = sum(v.get("category") == "Goggins" for v in videos)
    longevity_count = sum(v.get("category") == "Longevity" for v in videos)
    new_videos = sum((v.get("published") or "") >= fresh_cutoff for v in videos)
    print(f"Saved video library: {len(videos)} ({goggins_count} Goggins, {longevity_count} Longevity).")

    # --- Multi-source research. Each provider can fail independently. ---
    paper_candidates_count = 0
    newly_kept_papers = []
    try:
        paper_candidates = fetch_papers()
        paper_candidates_count = len(paper_candidates)
        # Cap total Gemini work while retaining a fair split between paper themes.
        paper_candidates.sort(key=lambda p: p.get("date", ""), reverse=True)
        selected = []
        for category in ("Longevity", "Toughness"):
            selected.extend([p for p in paper_candidates if p.get("guess") == category][:MAX_PAPERS_FOR_GEMINI // 2])
        # Keep IDs unique if they were found by more than one source.
        selected = list({p["id"]:p for p in selected}.values())
        if selected and gemini_available:
            paper_ok = judge_all(selected, labels, judge_papers, PAPER_BATCH)
            if paper_ok:
                for paper in selected:
                    label = labels.get(paper["id"])
                    if not label or not label.get("keep"): continue
                    if label.get("cat") not in ("Longevity", "Toughness") or not label.get("summary"): continue
                    newly_kept_papers.append({
                        "id":paper["id"], "title":paper["title"], "authors":paper["authors"],
                        "journal":paper["journal"], "date":paper["date"], "url":paper["url"],
                        "preprint":paper["preprint"], "category":label["cat"],
                        "summary":label["summary"], "evidence":label.get("evidence", "other"),
                        "source":paper.get("source", paper.get("journal", "")),
                    })
            else:
                print("Gemini paper review incomplete; existing papers will be retained and cache preserved.")
        elif selected:
            print("Gemini unavailable; existing papers retained.")
    except Exception as exc:
        print(f"Research aggregation failed; existing papers will be retained: {exc}")

    papers = merge_by_id(existing_papers, newly_kept_papers)
    papers.sort(key=lambda paper: paper.get("date", ""), reverse=True)
    write_json_atomic(papers_path, papers)
    save_labels(labels)
    print(f"Saved research library: {len(papers)} papers ({paper_candidates_count} raw candidates this run).")

    status = {
        "updated":datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "videos":len(videos), "new_videos":new_videos, "papers":len(papers),
        "video_candidates":len(candidates), "paper_candidates":paper_candidates_count,
        "gemini_ok":bool(gemini_available), "youtube_quota_exhausted":bool(YOUTUBE_QUOTA_EXHAUSTED),
        "video_categories":{"Goggins":goggins_count,"Longevity":longevity_count},
        "youtube_units_used_today":QUOTA["used"], "youtube_quota_day":QUOTA["day"],
    }
    write_json_atomic(status_path, status)
    print("Pipeline completed successfully.")


if __name__ == "__main__":
    main()
