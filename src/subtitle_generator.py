"""
Subtitle Generator Module
Generates word-level subtitle timestamps from voiceover audio using faster-whisper.
"""

import json
from pathlib import Path

from faster_whisper import WhisperModel
from loguru import logger


class SubtitleGenerator:
    """Transcribes audio and produces SRT subtitles with word-level timing."""

    def __init__(self, model_size: str = "base"):
        """Initialise the subtitle generator.

        Args:
            model_size: Whisper model size — one of "tiny", "base", "small", "medium".
                        Default is "base" for a good balance of speed and accuracy.
        """
        logger.info("Loading faster-whisper model (size={})", model_size)
        self.model = WhisperModel(model_size, device="cpu", compute_type="int8")
        logger.success("Whisper model loaded successfully")

    def generate_subtitles(self, audio_path: str, output_dir: str = "output") -> dict:
        """Generate subtitles from an audio file.

        Transcribes the audio, extracts word-level timestamps, and writes an
        SRT file alongside a JSON file with structured word timing data.

        Args:
            audio_path: Path to the input audio file (e.g. MP3).
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

        logger.info("Transcribing audio: {}", audio_path)
        words = self._transcribe(str(audio_path))
        logger.info("Transcription complete — {} words extracted", len(words))

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

    def _transcribe(self, audio_path: str) -> list[dict]:
        """Transcribe audio using faster-whisper and extract word-level timestamps.

        Args:
            audio_path: Path to the audio file.

        Returns:
            A list of dicts, each containing "word", "start", and "end" (in seconds).
        """
        segments, info = self.model.transcribe(audio_path, word_timestamps=True)
        logger.debug(
            "Detected language: {} (probability {:.2f})",
            info.language,
            info.language_probability,
        )

        words = []
        for segment in segments:
            if segment.words is None:
                continue
            for word in segment.words:
                words.append(
                    {
                        "word": word.word.strip(),
                        "start": round(word.start, 3),
                        "end": round(word.end, 3),
                    }
                )

        return words

    def _generate_srt(
        self, words: list[dict], output_path: str, words_per_group: int = 4
    ) -> str:
        """Group words into subtitle cues and write an SRT file.

        Args:
            words: List of word dicts with "word", "start", and "end".
            output_path: Destination path for the SRT file.
            words_per_group: Number of words per subtitle line (default 4).

        Returns:
            The path to the written SRT file.
        """
        if not words:
            logger.warning("No words to write — SRT file will be empty")
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
            lines.append("")  # blank line between cues
            index += 1

        srt_content = "\n".join(lines)
        Path(output_path).write_text(srt_content, encoding="utf-8")
        return output_path

    @staticmethod
    def _format_timestamp(seconds: float) -> str:
        """Convert a time value in seconds to SRT timestamp format.

        Args:
            seconds: Time in seconds (e.g. 62.450).

        Returns:
            Formatted string in HH:MM:SS,mmm format (e.g. "00:01:02,450").
        """
        if seconds < 0:
            seconds = 0.0
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = int(seconds % 60)
        millis = min(int(round((seconds - int(seconds)) * 1000)), 999)
        return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        logger.info("Usage: python subtitle_generator.py <audio_file> [output_dir]")
        logger.info("Example: python subtitle_generator.py output/test_voiceover.mp3 output")
        sys.exit(1)

    audio_file = sys.argv[1]
    out = sys.argv[2] if len(sys.argv) > 2 else "output"

    gen = SubtitleGenerator(model_size="base")
    result = gen.generate_subtitles(audio_file, output_dir=out)

    logger.info("SRT path : {}", result["srt_path"])
    logger.info("JSON path: {}", result["json_path"])
    logger.info("Total words: {}", len(result["words"]))

    # Print a preview of the first few words
    for w in result["words"][:10]:
        logger.info("  {:.3f}s - {:.3f}s : {}", w["start"], w["end"], w["word"])
