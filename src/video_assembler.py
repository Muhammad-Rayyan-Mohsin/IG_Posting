"""
Video Assembler Module
----------------------
Assembles the final Instagram Reel from AI-generated clips, stock footage,
voiceover audio, word-level subtitles, and optional background nasheed.

Uses MoviePy 2.x and FFmpeg for all video/audio processing.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from loguru import logger
from PIL import Image
from moviepy import (
    AudioFileClip,
    CompositeAudioClip,
    CompositeVideoClip,
    ImageClip,
    TextClip,
    VideoFileClip,
    concatenate_audioclips,
    concatenate_videoclips,
)

# Optional: Arabic text reshaping for correct subtitle rendering
try:
    import arabic_reshaper
    from bidi.algorithm import get_display

    _ARABIC_SUPPORT = True
except ImportError:
    _ARABIC_SUPPORT = False
    logger.warning(
        "arabic_reshaper / python-bidi not installed — "
        "Arabic subtitle reshaping will be disabled"
    )

# Project-level paths
_ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets"
_FONTS_DIR = _ASSETS_DIR / "fonts"


class VideoAssembler:
    """Assembles clips, voiceover, subtitles, and nasheed into a final MP4."""

    # Target canvas for Instagram Reels (9:16 portrait)
    TARGET_WIDTH = 1080
    TARGET_HEIGHT = 1920
    TARGET_FPS = 30

    # Subtitle styling defaults
    SUBTITLE_FONT_SIZE = 48
    SUBTITLE_COLOR = "white"
    SUBTITLE_STROKE_COLOR = "black"
    SUBTITLE_STROKE_WIDTH = 2
    SUBTITLE_Y_POSITION = 1550  # pixels from top (near bottom of 1920px frame)
    SUBTITLE_MAX_WIDTH = 900  # max text width before wrapping
    SUBTITLE_WORD_GROUP_SIZE = 4  # words per subtitle chunk
    SUBTITLE_BG_COLOR = (0, 0, 0)  # semi-transparent background colour (RGB)
    SUBTITLE_BG_OPACITY = 0.5

    # Audio mixing
    NASHEED_VOLUME_RATIO = 0.20  # nasheed at 20% of voiceover volume

    # Export settings
    EXPORT_CODEC = "libx264"
    EXPORT_AUDIO_CODEC = "aac"
    EXPORT_PRESET = "medium"
    EXPORT_BITRATE = "8000k"

    def __init__(self, output_dir: str = "output"):
        """
        Initialize the video assembler.

        Parameters
        ----------
        output_dir : str
            Directory where the final assembled MP4 will be saved.
        """
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # Resolve font path — prefer Amiri (Arabic-friendly), fall back to Arial
        self.font = self._resolve_font()

        logger.info("VideoAssembler initialized — output dir: {}", self.output_dir)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def assemble(
        self,
        clips: list[str],
        voiceover_path: str,
        words: list[dict],
        nasheed_path: str | None = None,
        output_filename: str = "final.mp4",
    ) -> str:
        """Assemble the final video.

        Parameters
        ----------
        clips : list[str]
            Ordered list of video clip file paths (AI + stock, already
            sequenced by the caller).
        voiceover_path : str
            Path to the voiceover MP3 file.
        words : list[dict]
            Word-level timestamps for subtitles. Each dict must contain:
            ``{"word": str, "start": float, "end": float}``.
        nasheed_path : str or None
            Optional path to background nasheed audio. If ``None`` or the
            file does not exist, only the voiceover will be used.
        output_filename : str
            Name for the final output file (default ``"final.mp4"``).

        Returns
        -------
        str
            Absolute path to the assembled final MP4.
        """
        logger.info(
            "Starting assembly — {} clips, voiceover={}, words={}, nasheed={}",
            len(clips),
            voiceover_path,
            len(words),
            nasheed_path or "none",
        )

        # Track all clips for cleanup in finally block
        voiceover = AudioFileClip(voiceover_path)
        video_clips = []
        subtitle_clips = []
        mixed_audio = None
        final = None

        try:
            # 1. Load voiceover to determine target duration
            target_duration = voiceover.duration
            logger.info("Voiceover duration: {:.1f}s", target_duration)

            # 2. Load, resize, and prepare all video clips
            video_clips = self._load_and_resize_clips(clips)
            if not video_clips:
                raise ValueError("No valid video clips could be loaded")

            # 3. Apply Ken Burns effect to short or static-looking clips
            video_clips = [self._apply_ken_burns(c) for c in video_clips]

            # 4. Concatenate all clips
            concatenated = concatenate_videoclips(video_clips, method="compose")
            logger.info("Concatenated duration: {:.1f}s", concatenated.duration)

            # 5. Trim or loop to match voiceover duration
            concatenated = self._match_duration(concatenated, target_duration)
            logger.info("Duration after matching: {:.1f}s", concatenated.duration)

            # 6. Mix audio (voiceover + optional nasheed)
            mixed_audio = self._mix_audio(voiceover, nasheed_path)

            # 7. Attach audio to the video
            concatenated = concatenated.with_audio(mixed_audio)

            # 8. Create subtitle overlays
            subtitle_clips = self._create_subtitle_clips(
                words, (self.TARGET_WIDTH, self.TARGET_HEIGHT)
            )

            # 9. Composite subtitles onto the video
            if subtitle_clips:
                final = CompositeVideoClip([concatenated] + subtitle_clips)
                logger.info("Added {} subtitle overlays", len(subtitle_clips))
            else:
                final = concatenated
                logger.info("No subtitles to overlay")

            # 10. Export
            output_path = str(self.output_dir / output_filename)
            logger.info("Exporting final video to {}", output_path)

            final.write_videofile(
                output_path,
                fps=self.TARGET_FPS,
                codec=self.EXPORT_CODEC,
                audio_codec=self.EXPORT_AUDIO_CODEC,
                preset=self.EXPORT_PRESET,
                bitrate=self.EXPORT_BITRATE,
                logger=None,  # suppress MoviePy's internal progress bar
            )

            final_path = str(Path(output_path).resolve())
            logger.success("Final video assembled: {}", final_path)
            return final_path

        finally:
            # Release all file handles even if an exception occurred
            all_to_close = video_clips + subtitle_clips + [
                c for c in [voiceover, mixed_audio, final] if c is not None
            ]
            self._close_clips(all_to_close)

    # ------------------------------------------------------------------
    # Clip loading & resizing
    # ------------------------------------------------------------------

    def _load_and_resize_clips(self, clip_paths: list[str]) -> list:
        """Load video files and resize them to the target 9:16 canvas.

        Parameters
        ----------
        clip_paths : list[str]
            Paths to video clip files.

        Returns
        -------
        list[VideoFileClip]
            Loaded and resized clips.
        """
        loaded: list = []

        for path in clip_paths:
            p = Path(path)
            if not p.exists():
                logger.warning("Clip not found, skipping: {}", path)
                continue

            try:
                clip = VideoFileClip(str(p))
                clip = self._resize_clip(clip)
                loaded.append(clip)
                logger.debug(
                    "Loaded clip: {} ({:.1f}s, {}x{})",
                    p.name,
                    clip.duration,
                    clip.w,
                    clip.h,
                )
            except Exception as exc:
                logger.error("Failed to load clip {}: {}", path, exc)
                continue

        logger.info("Loaded {}/{} clips successfully", len(loaded), len(clip_paths))
        return loaded

    def _resize_clip(self, clip, target_size: tuple[int, int] | None = None):
        """Resize and crop a clip to fill the target 9:16 frame.

        The clip is scaled so that it completely covers the target dimensions,
        then centre-cropped to the exact size.

        Parameters
        ----------
        clip
            A MoviePy ``VideoFileClip``.
        target_size : tuple[int, int], optional
            ``(width, height)`` — defaults to ``(TARGET_WIDTH, TARGET_HEIGHT)``.

        Returns
        -------
        VideoFileClip
            The resized and cropped clip.
        """
        tw, th = target_size or (self.TARGET_WIDTH, self.TARGET_HEIGHT)
        target_aspect = tw / th

        clip_aspect = clip.w / clip.h

        if clip_aspect > target_aspect:
            # Clip is wider than target — scale by height, crop width
            clip = clip.resized(height=th)
        else:
            # Clip is taller (or equal) — scale by width, crop height
            clip = clip.resized(width=tw)

        # Centre crop to exact target dimensions
        x_center = clip.w / 2
        y_center = clip.h / 2
        x1 = int(x_center - tw / 2)
        y1 = int(y_center - th / 2)

        clip = clip.cropped(x1=x1, y1=y1, width=tw, height=th)
        return clip

    # ------------------------------------------------------------------
    # Ken Burns effect
    # ------------------------------------------------------------------

    def _apply_ken_burns(self, clip, zoom_factor: float = 0.10):
        """Apply a gentle Ken Burns (slow zoom) effect to a clip.

        Creates a smooth zoom from 1.0x to (1 + zoom_factor)x over the
        clip's duration, keeping the centre in frame.

        Parameters
        ----------
        clip
            A MoviePy video clip.
        zoom_factor : float
            How much to zoom in over the clip's duration (default 10%).

        Returns
        -------
        VideoFileClip
            The clip with the Ken Burns effect applied.
        """
        if clip.duration is None or clip.duration <= 0:
            return clip

        base_w, base_h = clip.w, clip.h

        def ken_burns_filter(get_frame, t):
            """Apply progressive zoom for the current frame at time t."""
            progress = t / clip.duration if clip.duration > 0 else 0
            current_zoom = 1.0 + (zoom_factor * progress)

            frame = get_frame(t)

            # Calculate crop region for the zoom
            new_w = int(base_w / current_zoom)
            new_h = int(base_h / current_zoom)
            x1 = (base_w - new_w) // 2
            y1 = (base_h - new_h) // 2

            cropped = frame[y1 : y1 + new_h, x1 : x1 + new_w]

            # Resize back to original dimensions
            img = Image.fromarray(cropped)
            img = img.resize((base_w, base_h), Image.LANCZOS)
            return np.array(img)

        return clip.transform(ken_burns_filter)

    # ------------------------------------------------------------------
    # Duration matching
    # ------------------------------------------------------------------

    def _match_duration(self, clip, target_duration: float):
        """Trim or loop a clip to match the target duration exactly.

        Parameters
        ----------
        clip
            The concatenated video clip.
        target_duration : float
            Desired duration in seconds (typically the voiceover length).

        Returns
        -------
        VideoFileClip
            A clip whose duration matches ``target_duration``.
        """
        if clip.duration is None:
            logger.warning("Clip has no duration; returning as-is")
            return clip

        if abs(clip.duration - target_duration) < 0.1:
            # Close enough — no change needed
            return clip

        if clip.duration > target_duration:
            # Trim excess
            logger.debug(
                "Trimming clip from {:.1f}s to {:.1f}s",
                clip.duration,
                target_duration,
            )
            return clip.subclipped(0, target_duration)

        # Clip is shorter — loop it to fill the gap
        logger.debug(
            "Looping clip from {:.1f}s to {:.1f}s",
            clip.duration,
            target_duration,
        )
        loops_needed = int(target_duration / clip.duration) + 1
        looped = concatenate_videoclips([clip] * loops_needed, method="compose")
        return looped.subclipped(0, target_duration)

    # ------------------------------------------------------------------
    # Audio mixing
    # ------------------------------------------------------------------

    def _mix_audio(
        self, voiceover: 'AudioFileClip', nasheed_path: str | None = None
    ):
        """Mix voiceover with optional background nasheed.

        Parameters
        ----------
        voiceover : AudioFileClip
            The already-loaded voiceover audio clip.
        nasheed_path : str or None
            Path to the background nasheed audio. If ``None`` or the file
            does not exist, only the voiceover is returned.

        Returns
        -------
        AudioClip
            The mixed audio clip.
        """

        if not nasheed_path or not Path(nasheed_path).exists():
            if nasheed_path:
                logger.warning(
                    "Nasheed file not found at '{}', using voiceover only",
                    nasheed_path,
                )
            else:
                logger.info("No nasheed provided, using voiceover only")
            return voiceover

        logger.info("Mixing voiceover with nasheed: {}", nasheed_path)

        nasheed = AudioFileClip(nasheed_path)

        # Loop or trim nasheed to match voiceover duration
        if nasheed.duration < voiceover.duration:
            loops = int(voiceover.duration / nasheed.duration) + 1
            nasheed = concatenate_audioclips([nasheed] * loops)

        nasheed = nasheed.subclipped(0, voiceover.duration)

        # Reduce nasheed volume to NASHEED_VOLUME_RATIO of voiceover
        nasheed = nasheed.with_volume_scaled(self.NASHEED_VOLUME_RATIO)

        mixed = CompositeAudioClip([voiceover, nasheed])
        logger.info(
            "Audio mixed — voiceover {:.1f}s + nasheed at {:.0%} volume",
            voiceover.duration,
            self.NASHEED_VOLUME_RATIO,
        )
        return mixed

    # ------------------------------------------------------------------
    # Subtitles
    # ------------------------------------------------------------------

    def _create_subtitle_clips(
        self, words: list[dict], video_size: tuple[int, int]
    ) -> list:
        """Create word-by-word subtitle overlays with semi-transparent backgrounds.

        Words are grouped into chunks of ``SUBTITLE_WORD_GROUP_SIZE`` and
        displayed at the bottom of the frame.

        Parameters
        ----------
        words : list[dict]
            Word-level timestamps: ``[{"word": str, "start": float, "end": float}, ...]``
        video_size : tuple[int, int]
            ``(width, height)`` of the video canvas.

        Returns
        -------
        list
            List of MoviePy clips (text + background) to be composited.
        """
        if not words:
            return []

        subtitle_clips = []
        group_size = self.SUBTITLE_WORD_GROUP_SIZE
        vw, vh = video_size

        # Group words into chunks
        groups = []
        for i in range(0, len(words), group_size):
            chunk = words[i : i + group_size]
            group_text = " ".join(w.get("word", "") for w in chunk)
            group_start = chunk[0].get("start", 0)
            group_end = chunk[-1].get("end", group_start + 1)
            groups.append(
                {"text": group_text, "start": group_start, "end": group_end}
            )

        logger.info(
            "Creating {} subtitle groups from {} words",
            len(groups),
            len(words),
        )

        for group in groups:
            text = group["text"].strip()
            if not text:
                continue

            # Reshape Arabic text if needed
            display_text = self._reshape_arabic(text)

            duration = max(group["end"] - group["start"], 0.3)

            try:
                # Semi-transparent background rectangle
                bg_clip = self._create_subtitle_background(
                    duration=duration,
                    start=group["start"],
                )

                # Text overlay
                txt_clip = TextClip(
                    text=display_text,
                    font_size=self.SUBTITLE_FONT_SIZE,
                    color=self.SUBTITLE_COLOR,
                    font=self.font,
                    stroke_color=self.SUBTITLE_STROKE_COLOR,
                    stroke_width=self.SUBTITLE_STROKE_WIDTH,
                    size=(self.SUBTITLE_MAX_WIDTH, None),
                    method="caption",
                )
                txt_clip = (
                    txt_clip.with_position(("center", self.SUBTITLE_Y_POSITION))
                    .with_duration(duration)
                    .with_start(group["start"])
                )

                subtitle_clips.append(bg_clip)
                subtitle_clips.append(txt_clip)

            except Exception as exc:
                logger.warning(
                    "Failed to create subtitle for '{}': {}", text[:40], exc
                )
                continue

        return subtitle_clips

    def _create_subtitle_background(self, duration: float, start: float):
        """Create a semi-transparent dark rectangle behind subtitle text.

        Parameters
        ----------
        duration : float
            How long the background should be visible.
        start : float
            Start time in seconds.

        Returns
        -------
        VideoClip
            A semi-transparent background clip positioned behind the subtitle.
        """
        bg_width = self.SUBTITLE_MAX_WIDTH + 40  # padding
        bg_height = 80  # enough for ~2 lines of text

        # Create RGBA array (semi-transparent black)
        alpha = int(255 * self.SUBTITLE_BG_OPACITY)
        bg_array = np.zeros((bg_height, bg_width, 4), dtype=np.uint8)
        bg_array[:, :, 0] = self.SUBTITLE_BG_COLOR[0]
        bg_array[:, :, 1] = self.SUBTITLE_BG_COLOR[1]
        bg_array[:, :, 2] = self.SUBTITLE_BG_COLOR[2]
        bg_array[:, :, 3] = alpha

        # Convert to RGB with pre-multiplied alpha for MoviePy
        bg_rgb = np.zeros((bg_height, bg_width, 3), dtype=np.uint8)
        bg_rgb[:, :, 0] = self.SUBTITLE_BG_COLOR[0]
        bg_rgb[:, :, 1] = self.SUBTITLE_BG_COLOR[1]
        bg_rgb[:, :, 2] = self.SUBTITLE_BG_COLOR[2]

        bg_clip = ImageClip(bg_rgb)
        bg_clip = bg_clip.with_opacity(self.SUBTITLE_BG_OPACITY)

        # Position slightly above the text
        bg_x = (self.TARGET_WIDTH - bg_width) // 2
        bg_y = self.SUBTITLE_Y_POSITION - 10

        bg_clip = (
            bg_clip.with_position((bg_x, bg_y))
            .with_duration(duration)
            .with_start(start)
        )

        return bg_clip

    def _reshape_arabic(self, text: str) -> str:
        """Reshape Arabic text for correct visual rendering.

        Arabic characters need to be reshaped (connected forms) and
        reordered (right-to-left) for correct display in image-based
        subtitle rendering.

        Parameters
        ----------
        text : str
            The raw text (may contain Arabic, English, or mixed).

        Returns
        -------
        str
            The reshaped and bidi-reordered text, ready for rendering.
            Returns the original text unchanged if arabic_reshaper is
            not installed.
        """
        if not _ARABIC_SUPPORT:
            return text

        # Check if text contains Arabic characters
        has_arabic = any(
            "\u0600" <= ch <= "\u06FF" or "\u0750" <= ch <= "\u077F"
            for ch in text
        )

        if not has_arabic:
            return text

        try:
            reshaped = arabic_reshaper.reshape(text)
            display = get_display(reshaped)
            return display
        except Exception as exc:
            logger.warning("Arabic reshaping failed for '{}': {}", text[:40], exc)
            return text

    # ------------------------------------------------------------------
    # Font resolution
    # ------------------------------------------------------------------

    def _resolve_font(self) -> str:
        """Resolve the font to use for subtitle rendering.

        Checks for the Amiri font in ``assets/fonts/`` first (Arabic-friendly),
        then falls back to common system fonts.

        Returns
        -------
        str
            The font name or path.
        """
        # Check project assets for Amiri font
        amiri_candidates = [
            _FONTS_DIR / "Amiri-Regular.ttf",
            _FONTS_DIR / "Amiri.ttf",
            _FONTS_DIR / "amiri-regular.ttf",
        ]
        for font_path in amiri_candidates:
            if font_path.exists():
                logger.info("Using Amiri font: {}", font_path)
                return str(font_path)

        # Fall back to system fonts — only return a path if it actually exists
        system_fonts = [
            "/System/Library/Fonts/Supplemental/Arial.ttf",            # macOS
            "/usr/share/fonts/truetype/freefont/FreeSans.ttf",         # Linux (Dockerfile: fonts-freefont-ttf)
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",         # Linux (fonts-dejavu)
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",  # Linux (fonts-liberation)
            "C:/Windows/Fonts/arial.ttf",                               # Windows
        ]
        for font in system_fonts:
            if Path(font).exists():
                logger.info("Using system font: {}", font)
                return font

        logger.warning("No preferred font found — using generic 'Arial' name")
        return "Arial"

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    @staticmethod
    def _close_clips(clips: list) -> None:
        """Close a list of MoviePy clips to release file handles.

        Parameters
        ----------
        clips : list
            MoviePy clip objects. ``None`` entries are silently skipped.
        """
        for clip in clips:
            if clip is None:
                continue
            try:
                clip.close()
            except Exception:
                pass


# ----------------------------------------------------------------------
# CLI test entrypoint
# ----------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    logger.remove()
    logger.add(sys.stderr, level="DEBUG")

    # This test requires actual clip files, a voiceover, and optionally a nasheed.
    # It is intended to be run manually after generating content with the pipeline.

    sample_clips = [
        "output/clips/ai_clip_01.mp4",
        "output/clips/stock_clip_01.mp4",
        "output/clips/ai_clip_02.mp4",
        "output/clips/stock_clip_02.mp4",
    ]
    sample_voiceover = "output/test_voiceover.mp3"
    sample_nasheed = "assets/nasheed/background.mp3"

    # Sample word timestamps (as the voiceover generator would produce)
    sample_words = [
        {"word": "Have", "start": 0.0, "end": 0.3},
        {"word": "you", "start": 0.3, "end": 0.5},
        {"word": "ever", "start": 0.5, "end": 0.8},
        {"word": "wondered", "start": 0.8, "end": 1.3},
        {"word": "why", "start": 1.3, "end": 1.6},
        {"word": "the", "start": 1.6, "end": 1.8},
        {"word": "Prophet", "start": 1.8, "end": 2.3},
        {"word": "smiled", "start": 2.3, "end": 2.8},
        {"word": "so", "start": 2.8, "end": 3.0},
        {"word": "often?", "start": 3.0, "end": 3.5},
        {"word": "In", "start": 4.0, "end": 4.2},
        {"word": "a", "start": 4.2, "end": 4.3},
        {"word": "world", "start": 4.3, "end": 4.6},
        {"word": "that", "start": 4.6, "end": 4.8},
        {"word": "constantly", "start": 4.8, "end": 5.4},
        {"word": "tells", "start": 5.4, "end": 5.7},
        {"word": "us", "start": 5.7, "end": 5.9},
        {"word": "to", "start": 5.9, "end": 6.0},
        {"word": "stress", "start": 6.0, "end": 6.5},
    ]

    # Check if sample files exist before attempting assembly
    missing = [f for f in sample_clips if not Path(f).exists()]
    if missing:
        logger.error(
            "Cannot run test — missing clip files: {}. "
            "Generate clips first using video_generator.py.",
            missing,
        )
        sys.exit(1)

    if not Path(sample_voiceover).exists():
        logger.error(
            "Cannot run test — missing voiceover file: {}. "
            "Generate voiceover first using voiceover_generator.py.",
            sample_voiceover,
        )
        sys.exit(1)

    assembler = VideoAssembler(output_dir="output")

    logger.info("Starting test assembly...")
    result_path = assembler.assemble(
        clips=sample_clips,
        voiceover_path=sample_voiceover,
        words=sample_words,
        nasheed_path=sample_nasheed if Path(sample_nasheed).exists() else None,
        output_filename="test_final.mp4",
    )

    print("\n" + "=" * 60)
    print("ASSEMBLED VIDEO")
    print("=" * 60)
    print(f"Output: {result_path}")
