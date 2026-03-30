"""
Subtitle Generator Module
Generates word-level subtitle timestamps from the voiceover script text.

Uses a simple word-per-second estimation approach based on the voiceover
audio duration and the script text. No heavy ML models required.
"""

import json
from pathlib import Path

from loguru import logger
from moviepy import AudioFileClip


class SubtitleGenerator:
    """Produces SRT subtitles with word-level timing from script text and audio duration."""

    # Average speaking rate in words per second for the Edge-TTS voices used.
    # English Guy Neural at -5% rate ≈ 2.5 wps; Arabic Hamed at -10% ≈ 2.0 wps.
    DEFAULT_WPS = 2.4

    def generate_subtitles(self, script_text: str, audio_path: str, output_dir: str = "output") -> dict:
        """Generate subtitles from the script text and audio duration.

        Distributes word timings evenly across the audio duration based
        on the word count, producing SRT and JSON timing files.

        Args:
            script_text: The full narration script text.
            audio_path: Path to the voiceover audio file (used to get duration).
            output_dir: Directory to write the SRT and JSON output files.

        Returns:
            A dict with keys:
                - srt_path: Absolute path to the generated SRT file.
                - json_path: Absolute path to the generated JSON timing file.
                - words: List of dicts, each with "word", "start", and "end" keys.
        """
        audio_path = Path(audio_path)
        if not audio_path.exists():
            raise FileNotFoundError(f"Audio file not found: {audio_path}")

        out_dir = Path(output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

        stem = audio_path.stem
        srt_path = out_dir / f"{stem}.srt"
        json_path = out_dir / f"{stem}_words.json"

        # Get audio duration
        audio = AudioFileClip(str(audio_path))
        duration = audio.duration
        audio.close()
        logger.info("Audio duration: {:.1f}s", duration)

        # Clean script text — remove segment tags like [HOOK], [pause], etc.
        import re
        clean_text = re.sub(r'\[(?:HOOK|CONTEXT|CORE|REFLECTION|CTA|OUTRO|pause)\]', '', script_text)
        raw_words = clean_text.split()
        # Filter out empty tokens
        raw_words = [w.strip() for w in raw_words if w.strip()]

        if not raw_words:
            logger.warning("No words found in script text")
            srt_path.write_text("", encoding="utf-8")
            json.dump({"words": []}, json_path.open("w", encoding="utf-8"))
            return {"srt_path": str(srt_path.resolve()), "json_path": str(json_path.resolve()), "words": []}

        # Distribute words evenly across the audio duration
        word_count = len(raw_words)
        time_per_word = duration / word_count
        logger.info("Distributing {} words across {:.1f}s ({:.2f}s per word)", word_count, duration, time_per_word)

        words = []
        for i, word in enumerate(raw_words):
            start = round(i * time_per_word, 3)
            end = round((i + 1) * time_per_word, 3)
            words.append({"word": word, "start": start, "end": end})

        logger.info("Generated timing for {} words", len(words))

        # Write SRT
        srt_result = self._generate_srt(words, str(srt_path))
        logger.success("SRT file written to {}", srt_result)

        # Write word-level JSON
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump({"words": words}, f, indent=2, ensure_ascii=False)
        logger.success("Word timing JSON written to {}", json_path)

        return {
            "srt_path": str(srt_path.resolve()),
            "json_path": str(json_path.resolve()),
            "words": words,
        }

    def _generate_srt(
        self, words: list[dict], output_path: str, words_per_group: int = 4
    ) -> str:
        """Group words into subtitle cues and write an SRT file."""
        if not words:
            Path(output_path).write_text("", encoding="utf-8")
            return output_path

        lines: list[str] = []
        index = 1

        for i in range(0, len(words), words_per_group):
            group = words[i : i + words_per_group]
            start_time = group[0]["start"]
            end_time = group[-1]["end"]
            text = " ".join(w["word"] for w in group)

            lines.append(str(index))
            lines.append(
                f"{self._format_timestamp(start_time)} --> {self._format_timestamp(end_time)}"
            )
            lines.append(text)
            lines.append("")
            index += 1

        srt_content = "\n".join(lines)
        Path(output_path).write_text(srt_content, encoding="utf-8")
        return output_path

    @staticmethod
    def _format_timestamp(seconds: float) -> str:
        """Convert seconds to SRT timestamp format (HH:MM:SS,mmm)."""
        if seconds < 0:
            seconds = 0.0
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = int(seconds % 60)
        millis = min(int(round((seconds - int(seconds)) * 1000)), 999)
        return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"
