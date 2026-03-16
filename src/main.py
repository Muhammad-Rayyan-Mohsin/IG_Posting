"""
Automated Instagram Islamic Content Pipeline
=============================================
Runs daily via Railway cron. Generates an Islamic inspirational video
and posts it as an Instagram Reel.

Pipeline:
    1. Generate script          (Gemini 2.0 Flash)
    2. Generate voiceover       (Edge-TTS)
    3. Generate subtitles       (faster-whisper)
    4. Generate/fetch video clips (Sora 2 + Pexels)
    5. Assemble final video     (MoviePy + FFmpeg)
    6. Post to Instagram        (Graph API via Cloudflare R2)
    7. Log to content ledger    (Google Sheets)
"""

import os
import random
import sys
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
from voiceover_generator import VoiceoverGenerator
from video_generator import VideoGenerator
from video_assembler import VideoAssembler
from instagram_poster import InstagramPoster
from content_ledger import ContentLedger


def _check_env_vars(required: list[str]) -> list[str]:
    """Return a list of required environment variable names that are not set."""
    return [var for var in required if not os.environ.get(var)]


def run_pipeline():
    """Execute the full content pipeline.

    Each step is wrapped so that failures are caught, logged, and recorded
    in the content ledger before the process exits with a non-zero code.
    """
    load_dotenv()

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
    optional_vars = ["KIE_API_KEY", "PEXELS_API_KEY"]
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
        logger.info("Step 1/7 — Initializing content ledger")
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
        # Step 2: Generate script
        # ==============================================================
        logger.info("Step 2/7 — Generating script")
        script_gen = ScriptGenerator(api_key=os.environ["ANTHROPIC_API_KEY"])

        used_refs = ledger.get_used_references()
        last_hashtag_id = ledger.get_last_hashtag_set_id()
        script_data = script_gen.generate_script(
            used_references=used_refs,
            last_hashtag_set_id=last_hashtag_id,
        )

        logger.info(
            "Script generated — title: '{}', category: '{}'",
            script_data.get("title", "Untitled"),
            script_data.get("category", "Unknown"),
        )

        # Log to ledger with status "generated"
        ledger_row = ledger.log_entry(
            date=today,
            category=script_data.get("category", ""),
            title=script_data.get("title", ""),
            script=script_data.get("script", ""),
            sources=script_data.get("sources", []),
            hashtag_set_id=script_data.get("hashtag_set_id", 0),
        )
        logger.info("Ledger entry created at row {}", ledger_row)

        # ==============================================================
        # Step 3: Generate voiceover
        # ==============================================================
        logger.info("Step 3/7 — Generating voiceover")
        tts = VoiceoverGenerator(output_dir=str(output_dir))
        voiceover_path = tts.generate(
            text=script_data["script"],
            output_path=str(output_dir / "voiceover.mp3"),
        )
        logger.info("Voiceover saved: {}", voiceover_path)

        # ==============================================================
        # Step 4: Subtitles — skipped (short-form ambient storytelling,
        # no text overlays by design)
        # ==============================================================
        logger.info("Step 4/7 — Subtitles skipped (short-form format)")

        # ==============================================================
        # Step 5: Generate / fetch video clips
        # ==============================================================
        logger.info("Step 5/7 — Generating video clips")
        video_gen = VideoGenerator(
            kie_api_key=os.environ.get("KIE_API_KEY", ""),
            pexels_api_key=os.environ.get("PEXELS_API_KEY", ""),
            output_dir=str(output_dir),
        )

        visual_prompts = script_data.get("visual_prompts", [])
        clips_data = video_gen.generate_all_clips(visual_prompts)
        all_clips = clips_data.get("ai_clips", []) + clips_data.get("stock_clips", [])
        logger.info("Video clips ready: {} total", len(all_clips))

        # Fallback: if no clips were produced, use curated prompts from visual library
        if not all_clips:
            logger.warning("No clips generated — using fallback prompts from visual library")
            fallback_prompts = video_gen.get_fallback_prompts(count=4)
            fallback_clips = video_gen.fetch_stock_clips(fallback_prompts)
            all_clips = fallback_clips
            logger.info("Fallback produced {} clips", len(all_clips))

        if not all_clips:
            raise RuntimeError(
                "No video clips available — both AI generation and stock footage "
                "fallback produced zero clips. Check OPENAI_API_KEY and PEXELS_API_KEY."
            )

        # ==============================================================
        # Step 6: Assemble final video
        # ==============================================================
        logger.info("Step 6/7 — Assembling final video")
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
            logger.info("No nasheed found — video will use voiceover only")

        final_video = assembler.assemble(
            clips=all_clips,
            voiceover_path=voiceover_path,
            words=[],
            nasheed_path=nasheed_path,
            output_filename="final_reel.mp4",
            show_subtitles=False,
        )
        logger.info("Final video assembled: {}", final_video)

        # Update ledger
        ledger.update_status(ledger_row, "assembled", video_url="local")

        # ==============================================================
        # Step 7: Post to Instagram
        # ==============================================================
        logger.info("Step 7/7 — Posting to Instagram")
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
