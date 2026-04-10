"""
Automated Instagram Islamic Content Pipeline
=============================================
Runs daily via Railway cron. Generates an Islamic inspirational video
and posts it as an Instagram Reel.

Pipeline:
    1. Initialize content ledger  (Google Sheets deduplication guard)
    2. Generate script             (Claude — scene cards format)
    3. Generate video clips        (Sora 2 per scene via KIE AI)
    4. Assemble final video        (scene-card driven, nasheed audio)
    5. Post to Instagram           (Graph API via Cloudflare R2)
"""

import os
import random
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from loguru import logger

# ---------------------------------------------------------------------------
# Logging configuration
# ---------------------------------------------------------------------------
logger.remove()  # Remove the default stderr handler
logger.add(
    sys.stderr,
    level="INFO",
    format="{time:YYYY-MM-DD HH:mm:ss} | {level:<8} | {message}",
)
# File logging — one log per day, retained for 30 days
log_dir = Path(__file__).resolve().parent.parent / "logs"
log_dir.mkdir(parents=True, exist_ok=True)
logger.add(
    str(log_dir / "pipeline_{time:YYYY-MM-DD}.log"),
    rotation="1 day",
    retention="30 days",
    level="DEBUG",
    format="{time:YYYY-MM-DD HH:mm:ss} | {level:<8} | {message}",
)

# ---------------------------------------------------------------------------
# Module imports (all modules live in the same src/ directory)
# ---------------------------------------------------------------------------
from script_generator import ScriptGenerator
from video_generator import VideoGenerator
from video_assembler import VideoAssembler
from instagram_poster import InstagramPoster
from content_ledger import ContentLedger
from topic_intelligence import build_trending_context


def _check_env_vars(required: list[str]) -> list[str]:
    """Return a list of required environment variable names that are not set."""
    return [var for var in required if not os.environ.get(var)]


def run_pipeline():
    """Execute the full content pipeline.

    Each step is wrapped so that failures are caught, logged, and recorded
    in the content ledger before the process exits with a non-zero code.
    """
    load_dotenv()

    # ------------------------------------------------------------------
    # Anti-fingerprint: jitter the start time by 0-90 minutes and
    # randomly skip ~5% of runs so the posting cadence looks human,
    # not cron-deterministic. Meta's integrity classifiers flag
    # fixed-schedule automated accounts.
    # Set SKIP_JITTER=1 for local testing to bypass the delay.
    # ------------------------------------------------------------------
    if not os.environ.get("SKIP_JITTER"):
        skip_roll = random.random()
        if skip_roll < 0.05:
            logger.info(
                "Random skip today (5%% probability, roll={:.3f}) — "
                "breaking posting determinism",
                skip_roll,
            )
            sys.exit(0)

        jitter_seconds = random.randint(0, 90 * 60)  # 0 to 90 minutes
        logger.info(
            "Schedule jitter: waiting {:.0f} minutes before starting pipeline",
            jitter_seconds / 60,
        )
        time.sleep(jitter_seconds)
    else:
        logger.info("SKIP_JITTER set — bypassing schedule jitter for local testing")

    today = datetime.now().strftime("%Y-%m-%d")
    output_dir = Path(__file__).resolve().parent.parent / "output" / today
    output_dir.mkdir(parents=True, exist_ok=True)

    logger.info("=" * 60)
    logger.info("PIPELINE START — {}", today)
    logger.info("Output directory: {}", output_dir)
    logger.info("=" * 60)

    # ------------------------------------------------------------------
    # Pre-flight: verify critical environment variables
    # ------------------------------------------------------------------
    critical_vars = [
        "ANTHROPIC_API_KEY",
        "FAL_API_KEY",
        "GOOGLE_SHEETS_CREDENTIALS",
        "GOOGLE_SHEETS_SPREADSHEET_ID",
        "IG_USER_ID",
        "IG_ACCESS_TOKEN",
        "R2_ACCOUNT_ID",
        "R2_ACCESS_KEY",
        "R2_SECRET_KEY",
        "R2_BUCKET_NAME",
        "R2_PUBLIC_URL",
    ]
    missing = _check_env_vars(critical_vars)
    if missing:
        logger.error(
            "Missing required environment variables: {}",
            ", ".join(missing),
        )
        logger.error(
            "Set these in your .env file (local) or Railway variables (production)."
        )
        sys.exit(1)

    # Optional vars — warn but don't abort
    optional_vars = ["PEXELS_API_KEY", "OPENAI_API_KEY"]
    missing_optional = _check_env_vars(optional_vars)
    if missing_optional:
        logger.warning(
            "Optional environment variables not set: {} — "
            "some features may be limited",
            ", ".join(missing_optional),
        )

    ledger = None
    ledger_row = None

    try:
        # ==============================================================
        # Step 1: Initialize content ledger
        # ==============================================================
        logger.info("Step 1/5 — Initializing content ledger")
        ledger = ContentLedger(
            credentials_json=os.environ["GOOGLE_SHEETS_CREDENTIALS"],
            spreadsheet_id=os.environ["GOOGLE_SHEETS_SPREADSHEET_ID"],
        )
        logger.info("Content ledger ready")

        # ------------------------------------------------------------------
        # Deduplication guard: exit cleanly if today was already posted.
        # This prevents Railway's ON_FAILURE restart policy from double-posting
        # when a post-publish step (e.g., Sheets update) crashes.
        # ------------------------------------------------------------------
        recent = ledger.get_recent_entries(days=1)
        already_posted = any(
            r.get("Date") == today and r.get("Status") == "posted"
            for r in recent
        )
        if already_posted:
            logger.info(
                "Content for {} was already posted — exiting cleanly (Railway restart guard)",
                today,
            )
            sys.exit(0)

        # ==============================================================
        # Step 1.5: Fetch trending context (Google Autocomplete + Reddit)
        # ==============================================================
        logger.info("Step 1.5/5 — Fetching trending context")
        try:
            trending_context = build_trending_context()
            if trending_context:
                logger.info(
                    "Trending context ready ({} chars) — Claude will consider it",
                    len(trending_context),
                )
            else:
                logger.info("No trending data — Claude will use normal topic selection")
        except Exception as exc:
            logger.warning("Trending context fetch failed: {} — continuing without it", exc)
            trending_context = ""

        # ==============================================================
        # Step 2: Generate script
        # ==============================================================
        logger.info("Step 2/5 — Generating script")
        script_gen = ScriptGenerator(api_key=os.environ["ANTHROPIC_API_KEY"])

        used_refs = ledger.get_used_references()
        last_hashtag_id = ledger.get_last_hashtag_set_id()
        script_data = script_gen.generate_script(
            used_references=used_refs,
            last_hashtag_set_id=last_hashtag_id,
            trending_context=trending_context,
        )

        logger.info(
            "Script generated — title: '{}', category: '{}'",
            script_data.get("title", "Untitled"),
            script_data.get("category", "Unknown"),
        )

        # Build a plain-text preview from all scene text_lines
        scenes = script_data.get("scenes", [])
        script_preview = " ".join(
            line
            for scene in scenes
            for line in scene.get("text_lines", [])
        )

        # Log to ledger with status "generated"
        ledger_row = ledger.log_entry(
            date=today,
            category=script_data.get("category", ""),
            title=script_data.get("title", ""),
            script=script_preview,
            sources=script_data.get("sources", []),
            hashtag_set_id=script_data.get("hashtag_set_id", 0),
        )
        logger.info("Ledger entry created at row {}", ledger_row)

        # ==============================================================
        # Step 3: Generate video clips (one per scene via Sora 2)
        # ==============================================================
        logger.info("Step 3/5 — Generating video clips")
        video_gen = VideoGenerator(
            fal_api_key=os.environ["FAL_API_KEY"],
            pexels_api_key=os.environ.get("PEXELS_API_KEY", ""),
            output_dir=str(output_dir),
        )

        scene_bible = script_data.get("scene_bible", {})
        all_clips = video_gen.generate_all_clips(scenes=scenes, scene_bible=scene_bible)

        # Drop scenes whose clip failed or was skipped by the 90s cap so the
        # assembler receives matched-length scenes and clip_paths lists.
        paired = [(s, c) for s, c in zip(scenes, all_clips) if c]
        if paired:
            scenes, all_clips = [list(t) for t in zip(*paired)]
        else:
            scenes, all_clips = [], []
        logger.info("Video clips ready: {} usable (of {} scenes)", len(all_clips), len(script_data.get("scenes", [])))

        if not all_clips:
            logger.warning("No clips generated — using fallback prompts from visual library")
            fallback_prompts = video_gen.get_fallback_prompts(count=4)
            all_clips = video_gen.fetch_stock_clips(fallback_prompts)
            logger.info("Fallback produced {} clips", len(all_clips))
            # Fallback path produces clips without matching scene cards — build
            # minimal placeholder scenes so the assembler still has 1:1 pairing.
            scenes = [
                {
                    "id": i + 1,
                    "segment": "CORE",
                    "duration": 8,
                    "text_lines": [],
                    "emphasis_words": [],
                }
                for i in range(len(all_clips))
            ]

        if not all_clips:
            raise RuntimeError(
                "No video clips available — both Wan 2.5 generation and stock "
                "footage fallback produced zero clips. Check FAL_API_KEY and PEXELS_API_KEY."
            )

        # ==============================================================
        # Step 4: Assemble final video
        # ==============================================================
        logger.info("Step 4/5 — Assembling final video")
        assembler = VideoAssembler(output_dir=str(output_dir))

        # Look for a nasheed file in assets/nasheeds/
        nasheed_dir = Path(__file__).resolve().parent.parent / "assets" / "nasheeds"
        nasheed_files = (
            list(nasheed_dir.glob("*.mp3")) + list(nasheed_dir.glob("*.wav"))
            if nasheed_dir.exists()
            else []
        )
        nasheed_path = str(random.choice(nasheed_files)) if nasheed_files else None
        if nasheed_path:
            logger.info("Using nasheed: {}", nasheed_path)
        else:
            logger.info("No nasheed found — video will be assembled without background audio")

        final_video = assembler.assemble(
            scenes=scenes,
            clip_paths=all_clips,
            nasheed_path=nasheed_path,
            output_filename="final_reel.mp4",
        )
        logger.info("Final video assembled: {}", final_video)

        # Update ledger
        ledger.update_status(ledger_row, "assembled", video_url="local")

        # ==============================================================
        # Step 5: Post to Instagram
        # ==============================================================
        logger.info("Step 5/5 — Posting to Instagram")
        poster = InstagramPoster(
            ig_user_id=os.environ["IG_USER_ID"],
            ig_access_token=os.environ["IG_ACCESS_TOKEN"],
            r2_account_id=os.environ["R2_ACCOUNT_ID"],
            r2_access_key=os.environ["R2_ACCESS_KEY"],
            r2_secret_key=os.environ["R2_SECRET_KEY"],
            r2_bucket_name=os.environ["R2_BUCKET_NAME"],
            r2_public_url=os.environ["R2_PUBLIC_URL"],
        )

        # Build full caption with hashtags
        caption = script_data.get("caption", "")
        hashtags = script_data.get("hashtags", [])
        full_caption = f"{caption}\n\n{' '.join(hashtags)}" if hashtags else caption

        # Instagram enforces a 2200-character caption limit.
        if len(full_caption) > 2200:
            logger.warning(
                "Caption length {} exceeds Instagram's 2200-char limit — truncating",
                len(full_caption),
            )
            full_caption = full_caption[:2197] + "..."

        result = poster.post_reel(
            video_path=final_video,
            caption=full_caption,
        )

        if result["status"] == "posted":
            logger.info("Posted to Instagram! Post ID: {}", result.get("post_id"))
            ledger.update_status(
                ledger_row,
                "posted",
                video_url=result.get("video_url", ""),
                instagram_post_id=result.get("post_id", ""),
            )
        else:
            raise RuntimeError(
                f"Instagram posting failed — container may not have processed. "
                f"Result: {result}"
            )

        # ==============================================================
        # Done!
        # ==============================================================
        logger.info("=" * 60)
        logger.info("PIPELINE COMPLETE — {}", today)
        logger.info("=" * 60)

    except Exception as exc:
        logger.error("Pipeline failed: {}", exc)
        logger.error(traceback.format_exc())

        # Try to record the failure in the ledger
        if ledger and ledger_row:
            try:
                ledger.update_status(
                    ledger_row, "failed", error_message=str(exc)
                )
                logger.info("Failure recorded in content ledger (row {})", ledger_row)
            except Exception as ledger_exc:
                logger.error(
                    "Could not update ledger with error status: {}", ledger_exc
                )

        # Send failure notification if webhook URL is configured
        webhook_url = os.environ.get("FAILURE_WEBHOOK_URL")
        if webhook_url:
            try:
                import requests
                requests.post(webhook_url, json={
                    "text": f"Pipeline failed for {today}: {exc}",
                    "pipeline": "instagram-islamic-content",
                    "date": today,
                    "error": str(exc),
                }, timeout=10)
                logger.info("Failure notification sent to webhook")
            except Exception as notify_exc:
                logger.warning("Failed to send failure notification: {}", notify_exc)

        sys.exit(1)

    finally:
        # Keep all output files for debugging; cleanup can be added later
        pass


if __name__ == "__main__":
    run_pipeline()
