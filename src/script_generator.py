"""
Script Generation Module for Automated Islamic Instagram Content Pipeline.

Uses Google Gemini 2.0 Flash (free tier) to generate daily Islamic video scripts
with a 7-day rotating topic cycle, hashtag rotation, and content deduplication.
"""

import json
import re
from datetime import datetime
from pathlib import Path

import google.generativeai as genai
from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

# Project paths
CONFIG_DIR = Path(__file__).parent.parent / "config"


class ScriptGenerator:
    """Generates daily Islamic video scripts using Gemini 2.0 Flash."""

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
            api_key: Google Gemini API key.
        """
        # Configure Gemini
        genai.configure(api_key=api_key)
        self.model = genai.GenerativeModel("gemini-2.0-flash")
        logger.info("Gemini 2.0 Flash model initialized")

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

        # Pick the first candidate (deterministic rotation)
        # To rotate fairly, pick based on day-of-year mod available candidates
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

        Returns:
            A dict containing: title, script, visual_prompts, caption,
            hashtags, category, sources.

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
            f"Generate a script for today's Islamic video.\n\n"
            f"**Category:** {category}\n"
            f"**Date:** {datetime.now().strftime('%A, %B %d, %Y')}\n"
            f"{exclusion_text}\n\n"
            f"Respond ONLY with valid JSON in the exact format specified in your "
            f"system instructions. Do not include any text outside the JSON object. "
            f"The JSON must include these fields: "
            f"title, script, visual_prompts, caption, sources."
        )

        logger.info("Generating script for category: {}", category)
        logger.debug("Exclusion list has {} references", len(used_references))

        # Call Gemini
        response = self.model.generate_content(
            contents=user_prompt,
            generation_config=genai.types.GenerationConfig(
                temperature=0.8,
                max_output_tokens=4096,
            ),
            system_instruction=self.system_prompt,
        )

        # Validate that we got a response
        if not response or not response.text:
            raise ValueError("Gemini returned an empty response")

        logger.debug("Raw response length: {} chars", len(response.text))

        # Parse the response
        parsed = self._parse_response(response.text)

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
        Extract and validate JSON from the Gemini response.

        Handles responses wrapped in markdown code blocks (```json ... ```).

        Args:
            response_text: Raw text response from Gemini.

        Returns:
            Parsed and validated dict.

        Raises:
            ValueError: If JSON is malformed or missing required fields.
        """
        text = response_text.strip()

        # Strip markdown code fences if present
        # Handles ```json ... ``` and ``` ... ```
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
                        f"Could not parse JSON from Gemini response: {e}"
                    ) from e
            else:
                raise ValueError(
                    f"No JSON object found in Gemini response: {e}"
                ) from e

        if not isinstance(parsed, dict):
            raise ValueError(
                f"Expected a JSON object (dict), got {type(parsed).__name__}"
            )

        # Validate required fields
        required_fields = ["title", "script", "visual_prompts", "caption", "sources"]
        missing = [f for f in required_fields if f not in parsed]
        if missing:
            raise ValueError(
                f"Response JSON is missing required fields: {missing}. "
                f"Got keys: {list(parsed.keys())}"
            )

        # Basic type checks
        if not isinstance(parsed["title"], str) or not parsed["title"].strip():
            raise ValueError("Field 'title' must be a non-empty string")

        if not isinstance(parsed["script"], str) or not parsed["script"].strip():
            raise ValueError("Field 'script' must be a non-empty string")

        if not isinstance(parsed["visual_prompts"], list):
            raise ValueError("Field 'visual_prompts' must be a list")

        if not isinstance(parsed["caption"], str) or not parsed["caption"].strip():
            raise ValueError("Field 'caption' must be a non-empty string")

        if not isinstance(parsed["sources"], list):
            raise ValueError("Field 'sources' must be a list")

        logger.debug(
            "Parsed response: title='{}', visual_prompts={}, sources={}",
            parsed['title'],
            len(parsed['visual_prompts']),
            len(parsed['sources']),
        )

        return parsed


# ---------------------------------------------------------------------------
# CLI test entrypoint
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import os

    from dotenv import load_dotenv

    load_dotenv()

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        logger.error("GEMINI_API_KEY not set in environment or .env file")
        raise SystemExit(1)

    generator = ScriptGenerator(api_key=api_key)

    # Example: pretend we've used these references before
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
