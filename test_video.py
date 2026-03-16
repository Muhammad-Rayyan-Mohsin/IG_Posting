"""
Test video generator — Short Version branch.
Bypasses Google Sheets and Instagram posting.

Generates a complete 30-second test Reel using:
  - Claude Haiku (Anthropic API) for script (~75 words)
  - Edge-TTS for voiceover
  - KIE AI (Sora 2) for video clips (no subtitles)
  - Ambient nasheed at 40% volume
  - MoviePy for final assembly

Run with:
  /Library/Developer/CommandLineTools/usr/bin/python3.9 test_video.py
"""

import os
import sys
import traceback
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from loguru import logger

load_dotenv()

# ── output directory ──────────────────────────────────────────────────────────
OUTPUT_DIR = Path(__file__).parent / "output" / "test"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
CLIPS_DIR = OUTPUT_DIR / "clips"
CLIPS_DIR.mkdir(parents=True, exist_ok=True)

sys.path.insert(0, str(Path(__file__).parent / "src"))

logger.remove()
logger.add(sys.stderr, level="INFO", format="{time:HH:mm:ss} | {level:<8} | {message}")


# ── Step 1: Generate script ───────────────────────────────────────────────────

def generate_script() -> dict:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        logger.warning("ANTHROPIC_API_KEY not set — using hardcoded test script")
        return _hardcoded_script()

    from script_generator import ScriptGenerator

    gen = ScriptGenerator(api_key=api_key)
    try:
        script_data = gen.generate_script(used_references=[], last_hashtag_set_id=0)
        logger.success("Script generated: \"{}\"", script_data.get("title"))
        return script_data
    except Exception as exc:
        logger.warning("Script generation failed ({}), using hardcoded script", exc)
        return _hardcoded_script()


def _hardcoded_script() -> dict:
    return {
        "title": "The Smile — A Forgotten Sunnah",
        "category": "Hadith",
        "script": (
            "Have you ever considered that your smile could be an act of worship? "
            "The Prophet Muhammad, peace be upon him, said: "
            "'Your smiling in the face of your brother is charity.' "
            "Recorded in Sahih at-Tirmidhi, Hadith 1956. "
            "In a world full of stress, Islam reminds us that even the simplest gesture "
            "carries immense reward. "
            "When was the last time you smiled at a stranger — not because you had to, "
            "but because you wanted to spread warmth? "
            "Let your smile be your sadaqah today. "
            "May Allah fill your day with reasons to smile. Assalamu Alaikum."
        ),
        "caption": "Your smile is sadaqah — a forgotten sunnah with endless reward.",
        "hashtags": ["#islam", "#hadith", "#sunnah", "#islamicreminder", "#muslim"],
        "sources": ["Sahih at-Tirmidhi 1956"],
        "visual_prompts": [
            {
                "type": "ai",
                "description": (
                    "A grand mosque at golden hour, soft amber light streaming through "
                    "ornate arched windows, dust particles floating in shafts of light, "
                    "slow cinematic camera dolly, 4K, peaceful and reverent atmosphere"
                ),
                "duration": 8,
            },
            {
                "type": "ai",
                "description": (
                    "Close-up of an open Quran resting on a wooden stand, pages gently "
                    "illuminated by warm candlelight, soft bokeh background, slow zoom, "
                    "cinematic depth of field"
                ),
                "duration": 8,
            },
            {
                "type": "ai",
                "description": (
                    "Aerial view of a peaceful Islamic garden with geometric tile patterns, "
                    "a central fountain catching morning light, slow descent drone shot, "
                    "lush greenery, serene and meditative"
                ),
                "duration": 8,
            },
        ],
    }


# ── Step 2: Generate voiceover ────────────────────────────────────────────────

def generate_voiceover(script_text: str) -> str:
    from voiceover_generator import VoiceoverGenerator

    gen = VoiceoverGenerator(output_dir=str(OUTPUT_DIR))
    path = gen.generate(text=script_text, output_path=str(OUTPUT_DIR / "voiceover.mp3"))
    logger.success("Voiceover: {}", path)
    return path


# ── Step 3: Generate Sora clips via KIE AI ───────────────────────────────────

def generate_sora_clips(visual_prompts: list) -> list:
    kie_key = os.environ.get("KIE_API_KEY")
    if not kie_key:
        raise RuntimeError("KIE_API_KEY not set in .env — cannot generate Sora clips")

    from video_generator import VideoGenerator

    gen = VideoGenerator(
        kie_api_key=kie_key,
        pexels_api_key="",  # not needed — only using AI clips
        output_dir=str(OUTPUT_DIR),
    )

    # Only pass AI-type prompts; skip stock since no Pexels key
    ai_prompts = [p for p in visual_prompts if p.get("type") == "ai"]
    if not ai_prompts:
        raise RuntimeError("No AI visual prompts found in script data")

    logger.info(
        "Submitting {} Sora 2 Pro job(s) via KIE AI — this takes 15-40 min per clip",
        len(ai_prompts),
    )
    clips = gen.generate_ai_clips(ai_prompts)

    if not clips:
        raise RuntimeError("KIE returned no clips — check API key and job status")

    logger.success("{} Sora clip(s) downloaded", len(clips))
    return clips


# ── Step 5: Assemble final video ──────────────────────────────────────────────

def assemble_video(clips: list, voiceover_path: str) -> str:
    from video_assembler import VideoAssembler

    assembler = VideoAssembler(output_dir=str(OUTPUT_DIR))
    output_path = assembler.assemble(
        clips=clips,
        voiceover_path=voiceover_path,
        words=[],
        nasheed_path=None,
        output_filename="test_reel.mp4",
        show_subtitles=False,
    )
    return output_path


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    start = datetime.now()
    logger.info("=" * 55)
    logger.info("TEST VIDEO PIPELINE (Sora) — {}", start.strftime("%Y-%m-%d %H:%M"))
    logger.info("Output dir: {}", OUTPUT_DIR)
    logger.info("=" * 55)

    try:
        logger.info("Step 1/5 — Generating script")
        script_data = generate_script()

        logger.info("Step 2/5 — Generating voiceover")
        voiceover_path = generate_voiceover(script_data["script"])

        logger.info("Step 3/4 — Generating Sora clips via KIE AI (be patient...)")
        clips = generate_sora_clips(script_data.get("visual_prompts", []))

        logger.info("Step 4/4 — Assembling final video (no subtitles, ambient audio)")
        final_video = assemble_video(clips, voiceover_path)

        elapsed = (datetime.now() - start).total_seconds()
        logger.info("=" * 55)
        logger.success("DONE in {:.0f}s ({:.1f} min)", elapsed, elapsed / 60)
        logger.success("Final video: {}", final_video)
        logger.info("=" * 55)

        print(f"\n✓ Test video saved to:\n  {final_video}\n")

    except Exception as exc:
        logger.error("Test pipeline failed: {}", exc)
        logger.error(traceback.format_exc())
        sys.exit(1)


if __name__ == "__main__":
    main()
