"""
Topic Intelligence Module
-------------------------
Fetches real-time trending signals from free public sources and builds
a compact "trending context" string that the script generator injects
into Claude's prompt. This gives the pipeline awareness of what Muslims
are searching for and discussing TODAY — without any paid API.

Sources (all free, no API key):
    1. Google Search Autocomplete — what people are typing right now
    2. Reddit r/islam + related subs — what the community is discussing
    3. Islamic RSS feeds — current articles and news (optional)

Architecture: every network call is wrapped in try/except. If ALL sources
fail, the function returns an empty string and the pipeline falls back
to its normal category-based topic selection. Trending context is purely
advisory — Claude decides whether to use it.
"""

from __future__ import annotations

import random
from datetime import datetime

import requests
from loguru import logger


# ---------------------------------------------------------------------------
# Google Search Autocomplete
# ---------------------------------------------------------------------------

# Seeds that reveal what Muslims are actively searching for.
# Each seed produces ~8 suggestions from Google's autocomplete.
AUTOCOMPLETE_SEEDS = [
    "dua for",
    "hadith about",
    "quran verse about",
    "why does Allah",
    "islamic way to",
    "prophet Muhammad",
    "surah for",
    "is it haram to",
]

AUTOCOMPLETE_URL = "https://suggestqueries.google.com/complete/search"


def fetch_autocomplete(seeds: list[str] | None = None, max_per_seed: int = 5) -> list[str]:
    """Fetch Google Search autocomplete suggestions for Islamic seed phrases.

    Parameters
    ----------
    seeds : list[str], optional
        Override the default seed list. Each seed is sent as a partial
        query to Google's autocomplete endpoint.
    max_per_seed : int
        Max suggestions to keep per seed (default 5).

    Returns
    -------
    list[str]
        Flat list of autocomplete suggestions, deduplicated.
    """
    seeds = seeds or AUTOCOMPLETE_SEEDS
    # Randomize seed order and pick a subset to stay under rate limits
    selected = random.sample(seeds, min(5, len(seeds)))
    all_suggestions: list[str] = []

    for seed in selected:
        try:
            resp = requests.get(
                AUTOCOMPLETE_URL,
                params={"client": "firefox", "q": seed},
                timeout=5,
            )
            resp.raise_for_status()
            data = resp.json()
            suggestions = data[1] if len(data) > 1 else []
            # Filter out the seed itself and keep top N
            filtered = [
                s for s in suggestions[:max_per_seed]
                if s.strip().lower() != seed.strip().lower()
            ]
            all_suggestions.extend(filtered)
            logger.debug(
                "Autocomplete '{}' → {} suggestions", seed, len(filtered),
            )
        except Exception as exc:
            logger.debug("Autocomplete failed for '{}': {}", seed, exc)
            continue

    # Dedupe while preserving order
    seen = set()
    unique = []
    for s in all_suggestions:
        key = s.strip().lower()
        if key not in seen:
            seen.add(key)
            unique.append(s.strip())

    logger.info("Google Autocomplete: {} unique suggestions from {} seeds", len(unique), len(selected))
    return unique


# ---------------------------------------------------------------------------
# Reddit hot posts from Islamic subreddits
# ---------------------------------------------------------------------------

REDDIT_SUBREDDITS = [
    "islam",         # 2M+ members, general Islamic discussion
    "MuslimLounge",  # casual Muslim life topics
    "Quran",         # Quran-specific discussions
    "converts",      # revert questions (underserved angle)
]

REDDIT_USER_AGENT = "IslamicContentPipeline/1.0 (educational; automated)"


def fetch_reddit_hot(
    subreddits: list[str] | None = None,
    posts_per_sub: int = 5,
) -> list[dict]:
    """Fetch hot post titles from Islamic subreddits via Reddit's JSON API.

    No API key needed — Reddit serves JSON when you append ``.json`` to
    any URL. Rate limit is ~100 requests/min with a proper User-Agent.

    Parameters
    ----------
    subreddits : list[str], optional
        Override the default subreddit list.
    posts_per_sub : int
        Max posts to fetch per subreddit.

    Returns
    -------
    list[dict]
        List of ``{"title": str, "score": int, "subreddit": str}`` dicts,
        sorted by score descending.
    """
    subreddits = subreddits or REDDIT_SUBREDDITS
    all_posts: list[dict] = []

    for sub in subreddits:
        url = f"https://www.reddit.com/r/{sub}/hot.json"
        try:
            resp = requests.get(
                url,
                params={"limit": posts_per_sub + 2},  # +2 for stickied posts
                headers={"User-Agent": REDDIT_USER_AGENT},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            children = data.get("data", {}).get("children", [])

            for child in children:
                post = child.get("data", {})
                # Skip stickied/pinned posts (they're not trending)
                if post.get("stickied"):
                    continue
                all_posts.append({
                    "title": post.get("title", ""),
                    "score": post.get("score", 0),
                    "subreddit": sub,
                })

            logger.debug("Reddit r/{}: {} posts fetched", sub, len(children))
        except Exception as exc:
            logger.debug("Reddit r/{} failed: {}", sub, exc)
            continue

    # Sort by score descending, take top N
    all_posts.sort(key=lambda p: p["score"], reverse=True)
    top = all_posts[:15]

    logger.info(
        "Reddit: {} top posts across {} subreddits",
        len(top), len(subreddits),
    )
    return top


# ---------------------------------------------------------------------------
# Build the trending context string
# ---------------------------------------------------------------------------

def build_trending_context() -> str:
    """Fetch all trending signals and merge into a compact context string.

    The output is a ~200-word block that gets injected into Claude's
    user prompt. Claude uses it as advisory context — it's not a
    directive. If nothing is trending, returns an empty string and the
    pipeline falls back to its normal category rotation.

    Returns
    -------
    str
        A human-readable trending context block, or ``""`` if all
        sources failed.
    """
    sections: list[str] = []

    # 1. Google Autocomplete
    try:
        autocomplete = fetch_autocomplete()
        if autocomplete:
            # Take top 15 to keep context compact
            top_ac = autocomplete[:15]
            sections.append(
                "**What Muslims are searching for right now** "
                "(Google Autocomplete):\n"
                + "\n".join(f"- {s}" for s in top_ac)
            )
    except Exception as exc:
        logger.warning("Autocomplete fetch failed entirely: {}", exc)

    # 2. Reddit hot posts
    try:
        reddit_posts = fetch_reddit_hot()
        if reddit_posts:
            top_reddit = reddit_posts[:10]
            lines = [
                f"- [r/{p['subreddit']}] {p['title']} (score: {p['score']})"
                for p in top_reddit
            ]
            sections.append(
                "**What the Muslim community is discussing on Reddit** "
                "(hot posts today):\n"
                + "\n".join(lines)
            )
    except Exception as exc:
        logger.warning("Reddit fetch failed entirely: {}", exc)

    if not sections:
        logger.info("No trending data available — pipeline will use normal category rotation")
        return ""

    today = datetime.now().strftime("%A, %B %d, %Y")
    header = (
        f"=== TRENDING CONTEXT ({today}) ===\n"
        f"The following signals show what people are currently interested "
        f"in. If any of these themes naturally fit today's category, weave "
        f"the topic in. If none are relevant, ignore this section entirely "
        f"and pick your topic freely.\n\n"
    )

    context = header + "\n\n".join(sections)
    logger.info(
        "Trending context built: {} chars, {} sections",
        len(context), len(sections),
    )
    return context


# ---------------------------------------------------------------------------
# CLI test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import json

    logger.info("Testing topic intelligence module...")

    print("\n" + "=" * 60)
    print("1. GOOGLE AUTOCOMPLETE")
    print("=" * 60)
    ac = fetch_autocomplete()
    for s in ac:
        print(f"  • {s}")

    print("\n" + "=" * 60)
    print("2. REDDIT HOT POSTS")
    print("=" * 60)
    posts = fetch_reddit_hot()
    for p in posts:
        print(f"  [{p['score']:>5}] r/{p['subreddit']:15} {p['title'][:80]}")

    print("\n" + "=" * 60)
    print("3. MERGED TRENDING CONTEXT")
    print("=" * 60)
    context = build_trending_context()
    print(context if context else "(empty — all sources failed)")
    print(f"\nContext length: {len(context)} chars")
