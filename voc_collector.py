#!/usr/bin/env python3
"""
voc_collector.py — Voice-of-Customer collector for MyAnimeList, YouTube and Reddit.

Pulls public audience reviews/comments for a "baseline" and a "recent" cohort
and normalizes them into one industry-agnostic CSV for LLM persona analysis.

Output schema (voc_dataset.csv):
    comment_id        str   MAL_001 / YT_001 / RED_001 (sequential per source)
    cohort            str   "baseline" | "recent"
    source            str   "MyAnimeList" | "YouTube" | "Reddit"
    date              str   YYYY-MM-DD
    engagement_score  int   MAL reactions / YouTube likes / Reddit score
    raw_text          str   single-line cleaned text, >= MIN_WORDS words

Usage:
    pip install -r requirements.txt
    python voc_collector.py
"""

from __future__ import annotations

import html
import logging
import re
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Any, Iterator

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# =============================================================================
# CONFIG — paste your IDs and URLs here
# =============================================================================
CONFIG: dict[str, Any] = {
    "output_csv": "voc_dataset.csv",
    "min_words": 15,                 # comments shorter than this are dropped
    "request_timeout": 20,           # seconds per HTTP request
    "user_agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 voc-research/1.0"
    ),

    "myanimelist": {
        "enabled": False,
        "base_url": "https://api.jikan.moe/v4/anime/{mal_id}/reviews",
        "sleep_seconds": 1.5,        # Jikan rate limit courtesy delay
        "limit": 25,                 # max substantive reviews per entry
        "max_pages": 5,              # safety cap on pagination
        "include_preliminary": True, # include reviews written mid-airing
        "include_spoilers": True,
        "entries": [
            {"mal_id": 40748, "cohort": "baseline"},
            {"mal_id": 51009, "cohort": "recent"},
        ],
    },

    "youtube": {
        "enabled": True,
        "limit": 500,                 # max substantive comments per video
        "max_scan": 5000,            # stop after scanning this many raw comments
        "videos": [
            {"url": "https://www.youtube.com/watch?v=pkKu9hLT-t8", "cohort": "baseline"},
	    {"url": "https://www.youtube.com/watch?v=muSr2NVs1oM", "cohort": "baseline"},
            {"url": "https://www.youtube.com/watch?v=RYI-WG_HFV8", "cohort": "recent"},
	    {"url": "https://www.youtube.com/watch?v=MPfZhgLiK6w", "cohort": "recent"}
        ],
    },

    "reddit": {
        "enabled": False,
        "limit": 75,                 # max substantive comments per thread
        "max_depth": 1,              # 0 = top-level only, 1 = + direct replies
        "sleep_seconds": 2.0,        # pause between thread requests
        "excluded_authors": {"AutoModerator"},
        "excluded_bodies": {"[deleted]", "[removed]"},
        "threads": [
            {"url": "https://www.reddit.com/r/JuJutsuKaisen/comments/j3zbtm/jujutsu_kaisen_episode_1_discussion_thread/", "cohort": "baseline"},
            {"url": "https://www.reddit.com/r/anime/comments/18sz33o/jujutsu_kaisen_season_2_episode_23_discussion/", "cohort": "recent"},
        ],
    },
}

COLUMNS = ["comment_id", "cohort", "source", "date", "engagement_score", "raw_text"]
ID_PREFIX = {"MyAnimeList": "MAL", "YouTube": "YT", "Reddit": "RED"}
VALID_COHORTS = {"baseline", "recent"}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("voc")


# =============================================================================
# Shared record model + cleaning utilities
# =============================================================================
@dataclass
class Record:
    cohort: str
    source: str
    date: str
    engagement_score: int
    raw_text: str


_ZERO_WIDTH = re.compile(r"[\u200b\u200c\u200d\ufeff]")
_WHITESPACE = re.compile(r"\s+")


def clean_text(text: Any) -> str:
    """Unescape HTML entities, strip zero-width chars, collapse all whitespace
    (including newlines and tabs) to single spaces."""
    if not isinstance(text, str):
        return ""
    text = html.unescape(text)
    text = _ZERO_WIDTH.sub("", text)
    return _WHITESPACE.sub(" ", text).strip()


def word_count(text: str) -> int:
    return len(text.split())


def is_substantive(text: str, min_words: int | None = None) -> bool:
    return word_count(text) >= (min_words or CONFIG["min_words"])


def to_iso_date(value: Any) -> str:
    """Accept a Unix timestamp (int/float) or an ISO-8601 string; return YYYY-MM-DD."""
    try:
        if value is None or value == "":
            return ""
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(float(value), tz=timezone.utc).strftime("%Y-%m-%d")
        return pd.to_datetime(value, utc=True).strftime("%Y-%m-%d")
    except (ValueError, TypeError, OverflowError, OSError):
        return ""


_SUFFIX = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}


def parse_count(value: Any) -> int:
    """Turn '1.2K', '3,456', 17, None into ints."""
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    s = str(value).strip().upper().replace(",", "")
    if not s:
        return 0
    try:
        if s[-1] in _SUFFIX:
            return int(float(s[:-1]) * _SUFFIX[s[-1]])
        return int(float(s))
    except ValueError:
        return 0


def build_session() -> requests.Session:
    """HTTP session with retry/backoff on 429 + 5xx, honouring Retry-After."""
    session = requests.Session()
    retry = Retry(
        total=4,
        backoff_factor=2,  # 2s, 4s, 8s, 16s
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({"User-Agent": CONFIG["user_agent"], "Accept": "application/json"})
    return session


def is_placeholder(value: str) -> bool:
    return "PASTE" in str(value).upper()


# =============================================================================
# Source 1 — MyAnimeList (Jikan v4)
# =============================================================================
def parse_mal_review(item: dict, cohort: str) -> Record | None:
    text = clean_text(item.get("review"))
    if not is_substantive(text):
        return None
    reactions = item.get("reactions") or {}
    return Record(
        cohort=cohort,
        source="MyAnimeList",
        date=to_iso_date(item.get("date")),
        engagement_score=parse_count(reactions.get("overall", 0)),
        raw_text=text,
    )


def collect_myanimelist(session: requests.Session, cfg: dict) -> list[Record]:
    logger = logging.getLogger("voc.mal")
    records: list[Record] = []

    for entry in cfg["entries"]:
        mal_id, cohort = entry["mal_id"], entry["cohort"]
        url = cfg["base_url"].format(mal_id=mal_id)
        collected: list[Record] = []
        page = 1

        while len(collected) < cfg["limit"] and page <= cfg["max_pages"]:
            params = {
                "page": page,
                "preliminary": str(cfg["include_preliminary"]).lower(),
                "spoilers": str(cfg["include_spoilers"]).lower(),
            }
            try:
                resp = session.get(url, params=params, timeout=CONFIG["request_timeout"])
                resp.raise_for_status()
                payload = resp.json()
            except (requests.RequestException, ValueError) as exc:
                logger.error("MAL %s page %d failed: %s", mal_id, page, exc)
                break
            finally:
                time.sleep(cfg["sleep_seconds"])  # always respect Jikan rate limit

            for item in payload.get("data", []):
                rec = parse_mal_review(item, cohort)
                if rec:
                    collected.append(rec)
                    if len(collected) >= cfg["limit"]:
                        break

            if not (payload.get("pagination") or {}).get("has_next_page"):
                break
            page += 1

        logger.info("MAL id=%s (%s): %d reviews", mal_id, cohort, len(collected))
        records.extend(collected)

    return records


# =============================================================================
# Source 2 — YouTube trailer comments
# =============================================================================
def parse_youtube_comment(item: dict, cohort: str) -> Record | None:
    text = clean_text(item.get("text"))
    if not is_substantive(text):
        return None
    # 'time_parsed' is a Unix timestamp the library estimates from relative
    # strings like "2 years ago" — accurate to the day/week, not exact.
    return Record(
        cohort=cohort,
        source="YouTube",
        date=to_iso_date(item.get("time_parsed")),
        engagement_score=parse_count(item.get("votes")),
        raw_text=text,
    )


def collect_youtube(cfg: dict) -> list[Record]:
    logger = logging.getLogger("voc.youtube")
    try:
        from youtube_comment_downloader import SORT_BY_POPULAR, YoutubeCommentDownloader
    except ImportError:
        logger.error("youtube-comment-downloader not installed; skipping YouTube.")
        return []

    downloader = YoutubeCommentDownloader()
    records: list[Record] = []

    for video in cfg["videos"]:
        url, cohort = video["url"], video["cohort"]
        if is_placeholder(url):
            logger.warning("Skipping placeholder YouTube URL for cohort '%s'.", cohort)
            continue

        collected: list[Record] = []
        scanned = 0
        try:
            stream: Iterator[dict] = downloader.get_comments_from_url(url, sort_by=SORT_BY_POPULAR)
            for item in stream:
                scanned += 1
                if item.get("reply"):  # keep top-level comments only
                    continue
                rec = parse_youtube_comment(item, cohort)
                if rec:
                    collected.append(rec)
                if len(collected) >= cfg["limit"] or scanned >= cfg["max_scan"]:
                    break
        except Exception as exc:  # library raises bare RuntimeError/ValueError etc.
            logger.error("YouTube %s failed after %d comments: %s", url, scanned, exc)

        logger.info("YouTube %s (%s): %d comments (scanned %d)", url, cohort, len(collected), scanned)
        records.extend(collected)

    return records


# =============================================================================
# Source 3 — Reddit threads (public .json endpoint)
# =============================================================================
def reddit_json_url(thread_url: str) -> str:
    base = thread_url.split("?")[0].rstrip("/")
    return base if base.endswith(".json") else f"{base}.json"


def walk_reddit_comments(children: list[dict], max_depth: int, depth: int = 0) -> Iterator[tuple[dict, int]]:
    """Depth-first walk yielding (comment_data, depth). Skips 'more' stubs."""
    for child in children or []:
        if child.get("kind") != "t1":
            continue
        data = child.get("data") or {}
        yield data, depth
        if depth < max_depth:
            replies = data.get("replies")
            if isinstance(replies, dict):  # empty replies come back as ""
                yield from walk_reddit_comments(
                    replies.get("data", {}).get("children", []), max_depth, depth + 1
                )


def parse_reddit_comment(data: dict, cohort: str, cfg: dict) -> Record | None:
    author = data.get("author") or ""
    body = (data.get("body") or "").strip()
    if author in cfg["excluded_authors"] or body in cfg["excluded_bodies"]:
        return None
    if data.get("stickied") and author.lower().endswith("moderator"):
        return None
    text = clean_text(body)
    if not is_substantive(text):
        return None
    return Record(
        cohort=cohort,
        source="Reddit",
        date=to_iso_date(data.get("created_utc")),
        engagement_score=parse_count(data.get("score")),
        raw_text=text,
    )


def collect_reddit(session: requests.Session, cfg: dict) -> list[Record]:
    logger = logging.getLogger("voc.reddit")
    records: list[Record] = []

    for thread in cfg["threads"]:
        url, cohort = thread["url"], thread["cohort"]
        if is_placeholder(url):
            logger.warning("Skipping placeholder Reddit URL for cohort '%s'.", cohort)
            continue

        json_url = reddit_json_url(url)
        try:
            resp = session.get(
                json_url,
                params={"limit": 500, "sort": "top", "raw_json": 1},
                timeout=CONFIG["request_timeout"],
            )
            if resp.status_code == 403:
                logger.error("Reddit returned 403 for %s (blocked UA/IP or private sub).", json_url)
                continue
            resp.raise_for_status()
            payload = resp.json()
        except (requests.RequestException, ValueError) as exc:
            logger.error("Reddit %s failed: %s", json_url, exc)
            continue
        finally:
            time.sleep(cfg["sleep_seconds"])

        if not isinstance(payload, list) or len(payload) < 2:
            logger.error("Unexpected Reddit payload shape for %s", json_url)
            continue

        children = payload[1].get("data", {}).get("children", [])
        collected: list[Record] = []
        for data, _depth in walk_reddit_comments(children, cfg["max_depth"]):
            rec = parse_reddit_comment(data, cohort, cfg)
            if rec:
                collected.append(rec)
                if len(collected) >= cfg["limit"]:
                    break

        logger.info("Reddit %s (%s): %d comments", url, cohort, len(collected))
        records.extend(collected)

    return records


# =============================================================================
# Normalization + output
# =============================================================================
def normalize(records: list[Record]) -> pd.DataFrame:
    if not records:
        return pd.DataFrame(columns=COLUMNS)

    df = pd.DataFrame([asdict(r) for r in records])

    # Final guardrails (collectors already filter, this enforces the contract).
    df["raw_text"] = df["raw_text"].map(clean_text)
    df = df[df["raw_text"].map(is_substantive)]
    df = df[df["cohort"].isin(VALID_COHORTS)]
    df = df.drop_duplicates(subset=["source", "raw_text"])

    df["engagement_score"] = pd.to_numeric(df["engagement_score"], errors="coerce").fillna(0).astype(int)
    df["date"] = df["date"].fillna("").astype(str)

    # Stable ordering: source -> cohort (baseline first) -> engagement desc.
    source_order = {s: i for i, s in enumerate(ID_PREFIX)}
    df = (
        df.assign(
            _s=df["source"].map(source_order),
            _c=(df["cohort"] != "baseline").astype(int),
        )
        .sort_values(["_s", "_c", "engagement_score"], ascending=[True, True, False])
        .drop(columns=["_s", "_c"])
        .reset_index(drop=True)
    )

    seq = df.groupby("source").cumcount() + 1
    df["comment_id"] = [f"{ID_PREFIX[s]}_{n:03d}" for s, n in zip(df["source"], seq)]
    return df[COLUMNS]


def validate_config(cfg: dict) -> None:
    groups = [
        ("myanimelist", "entries"),
        ("youtube", "videos"),
        ("reddit", "threads"),
    ]
    for source, key in groups:
        for item in cfg[source][key]:
            if item.get("cohort") not in VALID_COHORTS:
                raise ValueError(f"{source}: cohort must be one of {VALID_COHORTS}, got {item.get('cohort')!r}")


def main() -> int:
    validate_config(CONFIG)
    session = build_session()
    records: list[Record] = []

    collectors = [
        ("myanimelist", lambda c: collect_myanimelist(session, c)),
        ("youtube", collect_youtube),
        ("reddit", lambda c: collect_reddit(session, c)),
    ]
    for name, fn in collectors:
        cfg = CONFIG[name]
        if not cfg.get("enabled", True):
            log.info("%s disabled in CONFIG; skipping.", name)
            continue
        try:
            records.extend(fn(cfg))
        except Exception:  # one broken source must not kill the run
            log.exception("Collector '%s' crashed; continuing with other sources.", name)

    df = normalize(records)
    if df.empty:
        log.error("No records collected — check CONFIG and network access.")
        return 1

    df.to_csv(CONFIG["output_csv"], index=False, encoding="utf-8")
    summary = df.groupby(["source", "cohort"]).size().unstack(fill_value=0)
    log.info("Wrote %d rows to %s\n%s", len(df), CONFIG["output_csv"], summary.to_string())
    return 0


if __name__ == "__main__":
    sys.exit(main())
