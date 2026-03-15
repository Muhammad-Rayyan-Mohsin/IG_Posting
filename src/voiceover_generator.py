"""
Voiceover Generator Module
Generates voiceover audio from scripts using Edge-TTS (Microsoft's free neural TTS).
"""

import asyncio
import json
from pathlib import Path

import edge_tts
from loguru import logger

# Default voice configuration used when config/voice_config.json is missing
DEFAULT_VOICE_CONFIG = {
    "english": {
        "voice": "en-US-GuyNeural",
        "rate": "-5%",
        "volume": "+0%",
        "pitch": "-2Hz",
    },
    "arabic": {
        "voice": "ar-SA-HamedNeural",
        "rate": "-10%",
        "volume": "+0%",
        "pitch": "-2Hz",
    },
}


class VoiceoverGenerator:
    """Generates voiceover MP3 audio from text using Edge-TTS."""

    def __init__(self, output_dir: str = "output"):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.config = self._load_voice_config()
        logger.info("VoiceoverGenerator initialized with output dir: {}", self.output_dir)

    def _load_voice_config(self) -> dict:
        """Load voice configuration from config/voice_config.json.

        Falls back to DEFAULT_VOICE_CONFIG if the file is missing or invalid.
        """
        config_path = Path(__file__).resolve().parent.parent / "config" / "voice_config.json"
        if config_path.exists():
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    config = json.load(f)
                logger.info("Loaded voice config from {}", config_path)
                return config
            except (json.JSONDecodeError, IOError) as exc:
                logger.warning("Failed to load voice config ({}), using defaults", exc)
        else:
            logger.info("No voice_config.json found at {}, using defaults", config_path)
        return DEFAULT_VOICE_CONFIG

    def _detect_language(self, text: str) -> str:
        """Detect whether the text is predominantly English or Arabic.

        Uses a simple heuristic: if more than 30% of the characters fall in
        the Arabic Unicode range, the text is treated as Arabic.

        Returns:
            "ar" for Arabic, "en" for English.
        """
        if not text:
            return "en"

        arabic_count = sum(
            1 for ch in text if "\u0600" <= ch <= "\u06FF" or "\u0750" <= ch <= "\u077F"
        )
        ratio = arabic_count / len(text)
        detected = "ar" if ratio > 0.30 else "en"
        logger.debug(
            "Language detection: {:.1%} Arabic characters -> {}",
            ratio,
            detected,
        )
        return detected

    async def _generate_async(
        self, text: str, output_path: str, voice: str | None = None
    ) -> str:
        """Generate voiceover audio asynchronously via Edge-TTS.

        Args:
            text: The script text to synthesise.
            output_path: Destination file path for the MP3.
            voice: Optional Edge-TTS voice name override.

        Returns:
            The absolute path to the saved MP3 file.
        """
        lang = self._detect_language(text)
        lang_key = "arabic" if lang == "ar" else "english"
        lang_config = self.config.get(lang_key, DEFAULT_VOICE_CONFIG[lang_key])

        selected_voice = voice or lang_config.get("voice", DEFAULT_VOICE_CONFIG[lang_key]["voice"])
        rate = lang_config.get("rate", "-5%")
        volume = lang_config.get("volume", "+0%")
        pitch = lang_config.get("pitch", "-2Hz")

        logger.info(
            "Generating voiceover — voice={}, rate={}, volume={}, pitch={}",
            selected_voice,
            rate,
            volume,
            pitch,
        )

        communicate = edge_tts.Communicate(
            text=text,
            voice=selected_voice,
            rate=rate,
            volume=volume,
            pitch=pitch,
        )
        await communicate.save(output_path)

        logger.success("Voiceover saved to {}", output_path)
        return str(Path(output_path).resolve())

    def generate(self, text: str, output_path: str, voice: str | None = None) -> str:
        """Synchronous wrapper around the async Edge-TTS generation.

        Args:
            text: The script text to synthesise.
            output_path: Destination file path for the MP3.
            voice: Optional Edge-TTS voice name override.

        Returns:
            The absolute path to the saved MP3 file.
        """
        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            # Already inside an async context — create a new event loop in a thread
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor() as pool:
                result = pool.submit(
                    asyncio.run,
                    self._generate_async(text, str(output), voice),
                ).result()
            return result

        return asyncio.run(self._generate_async(text, str(output), voice))


if __name__ == "__main__":
    sample_script = (
        "[HOOK] Have you ever wondered why the Prophet, peace be upon him, "
        "smiled so often? [pause] "
        "[CONTEXT] In a world that constantly tells us to stress, to worry, "
        "to rush — Islam gives us a beautiful reminder. [pause] "
        "[CORE] The Prophet Muhammad, peace be upon him, said: "
        "'Your smiling in the face of your brother is charity.' "
        "Sahih at-Tirmidhi, Hadith 1956. [pause] "
        "Think about that. Something as simple as a smile is an act of worship. "
        "[REFLECTION] When was the last time you smiled at a stranger, "
        "not because you had to, but because you wanted to spread warmth? [pause] "
        "[CTA] If this touched your heart, share it with someone who needs a reminder. "
        "Follow for daily Islamic inspiration. [pause] "
        "[OUTRO] May Allah fill your day with reasons to smile. Assalamu Alaikum."
    )

    generator = VoiceoverGenerator(output_dir="output")
    result_path = generator.generate(
        text=sample_script,
        output_path="output/test_voiceover.mp3",
    )
    logger.info("Test voiceover generated at: {}", result_path)
