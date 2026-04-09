"""
Script Generation Module for Automated Islamic Instagram Content Pipeline.

Uses Claude Haiku 4.5 (via Anthropic API) to generate daily Islamic video
scripts with a 7-day rotating topic cycle, hashtag rotation, and content
deduplication.

Output format: scene cards (3-5 scenes, 30-50 seconds total, 90s ceiling).
Per-scene durations must be exactly 5 or 10 (Wan 2.5 enum constraint).
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

import requests
from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

# Project paths
CONFIG_DIR = Path(__file__).parent.parent / "config"

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
MODEL = "claude-haiku-4-5-20251001"

VALID_SEGMENTS = {"HOOK", "CORE", "RESOLUTION"}


class ScriptGenerator:
    """Generates daily Islamic video scripts using Claude Haiku 4.5 via Anthropic API."""

    # 7-day topic rotation: Monday=0 ... Sunday=6
    CATEGORY_SCHEDULE = {
        0: "Quran Verses",
        1: "Hadith",
        2: "Stories of the Prophets",
        3: "Stories of the Companions",
        4: "Jummah Special",
        5: "Islamic Character",
        6: "Nature & Reflection",
    }

    def __init__(self, api_key: str):
        """
        Initialize the script generator.

        Args:
            api_key: Anthropic API key.
        """
        self.api_key = api_key
        self.headers = {
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }
        logger.info("Claude Haiku 4.5 via Anthropic API initialized (model={})", MODEL)

        # Load system prompt
        self.system_prompt = self._load_system_prompt()

        # Load hashtag sets
        self.hashtag_sets = self._load_hashtag_sets()

    def _load_system_prompt(self) -> str:
        """Load the system prompt from config/system_prompt.md."""
        prompt_path = CONFIG_DIR / "system_prompt.md"
        if not prompt_path.exists():
            logger.warning(
                "System prompt not found at {}. "
                "Using a minimal default prompt.",
                prompt_path,
            )
            return (
                "You are an Islamic content scriptwriter for short-form Instagram videos. "
                "Generate scripts that are accurate, respectful, and engaging. "
                "Always cite Quran verses and Hadith with proper references. "
                "Respond in valid JSON."
            )
        text = prompt_path.read_text(encoding="utf-8").strip()
        logger.info("Loaded system prompt from {} ({} chars)", prompt_path, len(text))
        return text

    def _load_hashtag_sets(self) -> list[dict]:
        """Load hashtag sets from config/hashtag_sets.json."""
        hashtag_path = CONFIG_DIR / "hashtag_sets.json"
        if not hashtag_path.exists():
            logger.warning(
                "Hashtag sets not found at {}. "
                "Using default hashtag sets.",
                hashtag_path,
            )
            return [
                {
                    "id": 1,
                    "hashtags": [
                        "#islam", "#quran", "#muslim", "#islamic",
                        "#allah", "#deen", "#sunnah", "#islamicreminder",
                        "#muslimcommunity", "#faithinallah",
                    ],
                },
                {
                    "id": 2,
                    "hashtags": [
                        "#islamicquotes", "#quranverses", "#hadith",
                        "#prophetmuhammad", "#jannah", "#dua",
                        "#islamicknowledge", "#tawakkul", "#sabr",
                        "#alhamdulillah",
                    ],
                },
                {
                    "id": 3,
                    "hashtags": [
                        "#islamicwisdom", "#muslimlife", "#ummah",
                        "#islamicart", "#dhikr", "#salah",
                        "#islamiccontent", "#tawheed", "#iman",
                        "#barakallah",
                    ],
                },
            ]

        with open(hashtag_path, "r", encoding="utf-8") as f:
            sets = json.load(f)
        logger.info("Loaded {} hashtag sets from {}", len(sets), hashtag_path)
        return sets

    def get_todays_category(self) -> str:
        """
        Get today's content category based on the day of the week.

        Returns:
            The category string for today (e.g. "Quran Verses" on Monday).
        """
        weekday = datetime.now().weekday()  # Monday=0, Sunday=6
        category = self.CATEGORY_SCHEDULE[weekday]
        logger.info(
            "Today is {} -> category: {}", datetime.now().strftime('%A'), category
        )
        return category

    def get_hashtag_set(self, last_used_set_id: int = 0) -> dict:
        """
        Select a hashtag set, ensuring it differs from the last used one.

        Args:
            last_used_set_id: The ID of the most recently used hashtag set.
                Pass 0 or None if unknown.

        Returns:
            A dict with 'id' and 'hashtags' keys.
        """
        if not self.hashtag_sets:
            logger.warning("No hashtag sets available, returning empty set")
            return {"id": 0, "hashtags": []}

        # Filter out the last used set (if applicable)
        candidates = [
            s for s in self.hashtag_sets
            if s.get("id") != last_used_set_id
        ]

        # If filtering removed everything (e.g. only one set exists), use all
        if not candidates:
            candidates = self.hashtag_sets

        # Rotate fairly based on day-of-year
        day_of_year = datetime.now().timetuple().tm_yday
        selected = candidates[day_of_year % len(candidates)]

        logger.info(
            "Selected hashtag set #{} (last used: #{})",
            selected.get('id'),
            last_used_set_id,
        )
        return selected

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        retry=retry_if_exception_type((Exception,)),
        before_sleep=lambda retry_state: logger.warning(
            "Retry attempt {} after error: {}",
            retry_state.attempt_number,
            retry_state.outcome.exception(),
        ),
    )
    def generate_script(self, used_references: list[str] | None = None, last_hashtag_set_id: int = 0) -> dict:
        """
        Generate a complete Islamic video script for today's category.

        Args:
            used_references: List of previously used content references
                (e.g. Quran verse refs, Hadith numbers) to avoid repetition.
            last_hashtag_set_id: ID of the most recently used hashtag set.

        Returns:
            A dict containing: title, scene_bible, scenes, caption,
            sources, hashtags, hashtag_set_id, category, generated_at.

        Raises:
            ValueError: If the response cannot be parsed into valid JSON
                or is missing required fields.
            Exception: On API errors (will be retried up to 3 times).
        """
        category = self.get_todays_category()
        used_references = used_references or []

        # Build the user prompt
        exclusion_text = ""
        if used_references:
            exclusion_text = (
                "\n\n**IMPORTANT — Do NOT use any of these previously used references "
                "(pick something fresh):**\n"
                + "\n".join(f"- {ref}" for ref in used_references)
            )

        user_prompt = (
            f"Generate a scene-card script for today's Islamic Instagram Reel.\n\n"
            f"**Category:** {category}\n"
            f"**Date:** {datetime.now().strftime('%A, %B %d, %Y')}\n"
            f"{exclusion_text}\n\n"
            f"The video must be 30-50 seconds total (hard ceiling 90s), composed "
            f"of 3-5 scenes. **Each scene's duration field MUST be exactly 5 or 10** "
            f"(Wan 2.5 only supports these two clip lengths). "
            f"Each scene is a self-contained visual moment — no continuous narration. "
            f"Use short, impactful text lines for on-screen display and rich visual "
            f"prompts for AI video generation. Every visual_prompt must include "
            f"concrete audio cues (wind, water, birds, stone reverb, etc.) in its "
            f"audio_direction — Wan 2.5 synthesizes native audio from these cues.\n\n"
            f"Respond ONLY with valid JSON in the exact format specified in your "
            f"system instructions. Do not include any text outside the JSON object. "
            f"The JSON must include these top-level fields: "
            f"title, scene_bible, scenes, caption, sources."
        )

        logger.info("Generating script for category: {}", category)
        logger.debug("Exclusion list has {} references", len(used_references))

        # Call Anthropic Messages API
        payload = {
            "model": MODEL,
            "system": self.system_prompt,
            "messages": [
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.8,
            "max_tokens": 4096,
        }

        response = requests.post(
            ANTHROPIC_URL,
            headers=self.headers,
            json=payload,
            timeout=60,
        )
        response.raise_for_status()

        body = response.json()
        response_text = body["content"][0]["text"]

        if not response_text:
            raise ValueError("Anthropic API returned an empty response")

        logger.debug("Raw response length: {} chars", len(response_text))

        # Parse the response
        parsed = self._parse_response(response_text)

        # Attach metadata
        parsed["category"] = category
        parsed["generated_at"] = datetime.now().isoformat()

        # Attach hashtags (get set, avoiding last used)
        hashtag_set = self.get_hashtag_set(last_used_set_id=last_hashtag_set_id)
        parsed["hashtags"] = hashtag_set.get("hashtags", [])
        parsed["hashtag_set_id"] = hashtag_set.get("id", 0)

        logger.info(
            "Script generated successfully: \"{}\"", parsed.get('title', 'Untitled')
        )
        return parsed

    def _parse_response(self, response_text: str) -> dict:
        """
        Extract and validate JSON from the model response.

        Handles responses wrapped in markdown code blocks (```json ... ```).

        Validates the scene-card schema:
          - Required top-level fields: title, scene_bible, scenes, caption, sources
          - scene_bible fields: time_of_day, color_anchors (2-3 items), material_palette,
            film_look, ambient_sound_base
          - scenes: list of 3-5 dicts, each with id, segment, duration (5 or 10),
            text_lines, emphasis_words, visual_prompt, camera, color_palette,
            audio_direction; lighting is optional
          - Total scene duration: 30-50 seconds target, 90 seconds hard ceiling
          - sources: list

        Args:
            response_text: Raw text response from the model.

        Returns:
            Parsed and validated dict.

        Raises:
            ValueError: If JSON is malformed or missing required fields.
        """
        text = response_text.strip()

        # Strip markdown code fences if present
        code_block_match = re.search(
            r"```(?:json)?\s*\n?(.*?)\n?\s*```",
            text,
            re.DOTALL,
        )
        if code_block_match:
            text = code_block_match.group(1).strip()

        # Attempt JSON parse
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as e:
            logger.error("Failed to parse JSON from response: {}", e)
            logger.debug("Response text (first 500 chars): {}", text[:500])

            # Last-resort: try to find a JSON object in the text
            obj_match = re.search(r"\{.*\}", text, re.DOTALL)
            if obj_match:
                try:
                    parsed = json.loads(obj_match.group(0))
                    logger.info("Recovered JSON via regex extraction")
                except json.JSONDecodeError:
                    raise ValueError(
                        f"Could not parse JSON from model response: {e}"
                    ) from e
            else:
                raise ValueError(
                    f"No JSON object found in model response: {e}"
                ) from e

        if not isinstance(parsed, dict):
            raise ValueError(
                f"Expected a JSON object (dict), got {type(parsed).__name__}"
            )

        # --- Validate top-level required fields ---
        required_top = ["title", "scene_bible", "scenes", "caption", "sources"]
        missing_top = [f for f in required_top if f not in parsed]
        if missing_top:
            raise ValueError(
                f"Response JSON is missing required top-level fields: {missing_top}. "
                f"Got keys: {list(parsed.keys())}"
            )

        if not isinstance(parsed["title"], str) or not parsed["title"].strip():
            raise ValueError("Field 'title' must be a non-empty string")

        if not isinstance(parsed["caption"], str) or not parsed["caption"].strip():
            raise ValueError("Field 'caption' must be a non-empty string")

        if not isinstance(parsed["sources"], list):
            raise ValueError("Field 'sources' must be a list")

        # --- Validate scene_bible ---
        scene_bible = parsed["scene_bible"]
        if not isinstance(scene_bible, dict):
            raise ValueError("Field 'scene_bible' must be an object")

        required_bible_fields = [
            "time_of_day", "color_anchors", "material_palette",
            "film_look", "ambient_sound_base",
        ]
        missing_bible = [f for f in required_bible_fields if f not in scene_bible]
        if missing_bible:
            raise ValueError(
                f"'scene_bible' is missing required fields: {missing_bible}"
            )

        if not isinstance(scene_bible["time_of_day"], str):
            raise ValueError("'scene_bible.time_of_day' must be a string")

        color_anchors = scene_bible["color_anchors"]
        if not isinstance(color_anchors, list) or not (2 <= len(color_anchors) <= 3):
            raise ValueError(
                "'scene_bible.color_anchors' must be a list with 2-3 items, "
                f"got {len(color_anchors) if isinstance(color_anchors, list) else type(color_anchors).__name__}"
            )

        if not isinstance(scene_bible["material_palette"], list):
            raise ValueError("'scene_bible.material_palette' must be a list")

        if not isinstance(scene_bible["film_look"], str):
            raise ValueError("'scene_bible.film_look' must be a string")

        if not isinstance(scene_bible["ambient_sound_base"], str):
            raise ValueError("'scene_bible.ambient_sound_base' must be a string")

        # --- Validate scenes ---
        scenes = parsed["scenes"]
        if not isinstance(scenes, list) or not (3 <= len(scenes) <= 5):
            raise ValueError(
                f"'scenes' must be a list of 3-5 items, "
                f"got {len(scenes) if isinstance(scenes, list) else type(scenes).__name__}"
            )

        required_scene_fields = [
            "id", "segment", "duration", "narration", "text_lines",
            "emphasis_words", "visual_prompt", "camera",
            "color_palette", "audio_direction",
        ]

        for idx, scene in enumerate(scenes):
            if not isinstance(scene, dict):
                raise ValueError(f"scenes[{idx}] must be an object")

            missing_scene = [f for f in required_scene_fields if f not in scene]
            if missing_scene:
                raise ValueError(
                    f"scenes[{idx}] is missing required fields: {missing_scene}"
                )

            if not isinstance(scene["id"], int):
                raise ValueError(f"scenes[{idx}].id must be an int")

            if scene["segment"] not in VALID_SEGMENTS:
                raise ValueError(
                    f"scenes[{idx}].segment must be one of {VALID_SEGMENTS}, "
                    f"got '{scene['segment']}'"
                )

            duration = scene["duration"]
            if not isinstance(duration, int) or duration not in (5, 10):
                raise ValueError(
                    f"scenes[{idx}].duration must be exactly 5 or 10 (Wan 2.5 enum), "
                    f"got {duration!r}"
                )

            narration = scene.get("narration")
            if not isinstance(narration, str) or not narration.strip():
                raise ValueError(
                    f"scenes[{idx}].narration must be a non-empty string"
                )
            # Pace sanity-check: ~2.2 words/sec. 5s scenes: 8-11 words, 10s: 18-22.
            word_count = len(narration.split())
            if duration == 5 and not (6 <= word_count <= 13):
                logger.warning(
                    "scenes[{}].narration has {} words for a 5s scene — expected 8-11",
                    idx, word_count,
                )
            elif duration == 10 and not (15 <= word_count <= 26):
                logger.warning(
                    "scenes[{}].narration has {} words for a 10s scene — expected 18-22",
                    idx, word_count,
                )

            if not isinstance(scene["text_lines"], list):
                raise ValueError(f"scenes[{idx}].text_lines must be a list")

            if not isinstance(scene["emphasis_words"], list):
                raise ValueError(f"scenes[{idx}].emphasis_words must be a list")

            if not isinstance(scene["visual_prompt"], str) or not scene["visual_prompt"].strip():
                raise ValueError(f"scenes[{idx}].visual_prompt must be a non-empty string")

            if not isinstance(scene["camera"], str) or not scene["camera"].strip():
                raise ValueError(f"scenes[{idx}].camera must be a non-empty string")

            if not isinstance(scene["color_palette"], list):
                raise ValueError(f"scenes[{idx}].color_palette must be a list")

            if not isinstance(scene["audio_direction"], str) or not scene["audio_direction"].strip():
                raise ValueError(f"scenes[{idx}].audio_direction must be a non-empty string")

        # --- Validate total duration ---
        total_duration = sum(scene["duration"] for scene in scenes)
        if total_duration > 90:
            raise ValueError(
                f"Total scene duration must be ≤90 seconds, got {total_duration}s"
            )
        if total_duration < 15:
            raise ValueError(
                f"Total scene duration must be at least 15 seconds, got {total_duration}s"
            )

        logger.debug(
            "Parsed response: title='{}', scenes={}, total_duration={}s, sources={}",
            parsed["title"],
            len(scenes),
            total_duration,
            len(parsed["sources"]),
        )

        return parsed


# ---------------------------------------------------------------------------
# CLI test entrypoint
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import os

    from dotenv import load_dotenv

    load_dotenv()

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        logger.error("ANTHROPIC_API_KEY not set in environment or .env file")
        raise SystemExit(1)

    generator = ScriptGenerator(api_key=api_key)

    sample_used = [
        "Surah Al-Fatiha (1:1-7)",
        "Sahih Bukhari 1",
    ]

    logger.info("Starting test script generation...")
    script = generator.generate_script(used_references=sample_used)

    print("\n" + "=" * 60)
    print("GENERATED SCRIPT")
    print("=" * 60)
    print(json.dumps(script, indent=2, ensure_ascii=False))
