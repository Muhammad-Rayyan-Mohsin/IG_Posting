"""
Video Assembler Module
----------------------
Assembles the final Instagram Reel from scene-card-driven AI-generated clips.

Architecture: scene-card-driven (not voiceover-driven).
- Duration is determined by the sum of scene card durations.
- Each clip's native Sora 2 audio is the primary soundscape (80% volume).
- Text overlays are rendered via Pillow (not MoviePy TextClip).
- Optional nasheed mixed at 20% on top.

Uses MoviePy 2.x and FFmpeg for all video/audio processing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
from loguru import logger
from PIL import Image, ImageDraw, ImageFont
from moviepy import (
    AudioFileClip,
    CompositeAudioClip,
    CompositeVideoClip,
    ImageClip,
    VideoFileClip,
    concatenate_audioclips,
    concatenate_videoclips,
)
from moviepy.video.fx import FadeIn

# Optional: Arabic text reshaping for correct text rendering
try:
    import arabic_reshaper
    from bidi.algorithm import get_display

    _ARABIC_SUPPORT = True
except ImportError:
    _ARABIC_SUPPORT = False
    logger.warning(
        "arabic_reshaper / python-bidi not installed — "
        "Arabic text reshaping will be disabled"
    )

# Project-level paths
_ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets"
_FONTS_DIR = _ASSETS_DIR / "fonts"


class VideoAssembler:
    """Assembles scene-card-driven clips with Sora 2 audio and Pillow text overlays into a final MP4."""

    # Target canvas for Instagram Reels (9:16 portrait)
    TARGET_WIDTH = 1080
    TARGET_HEIGHT = 1920
    TARGET_FPS = 30

    # Text overlay styling
    TEXT_FONT_SIZE_NORMAL = 64
    TEXT_FONT_SIZE_HOOK = 80
    TEXT_COLOR = (255, 255, 255)           # white
    TEXT_STROKE_COLOR = (20, 20, 20)       # near-black stroke
    TEXT_STROKE_WIDTH = 3
    TEXT_EMPHASIS_COLOR = (212, 165, 116)  # gold #D4A574
    TEXT_Y_CENTER = 880                    # vertical center for text block (px from top, 1920px frame)
    TEXT_MAX_WIDTH = 900                   # max line width before wrapping
    TEXT_BG_OPACITY = 0.70                 # 70% opaque background rectangle
    TEXT_BG_CORNER_RADIUS = 16            # rounded corner radius (px)
    TEXT_FADE_IN_DURATION = 0.25          # seconds for fade-in per text card

    # Audio mixing
    SORA_AUDIO_VOLUME = 0.80   # Sora 2 clip audio at 80%
    NASHEED_VOLUME = 0.20       # optional nasheed at 20%

    # Export settings
    EXPORT_CODEC = "libx264"
    EXPORT_AUDIO_CODEC = "aac"
    EXPORT_PRESET = "medium"
    EXPORT_BITRATE = "10000k"

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

        # Resolve font path — prefer Cormorant-SemiBold, fall back to serif, then Arial
        self.font_path = self._resolve_font()

        logger.info("VideoAssembler initialized — output dir: {}", self.output_dir)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def assemble(
        self,
        scenes: list[dict],
        clip_paths: list[str],
        nasheed_path: str | None = None,
        output_filename: str = "final.mp4",
    ) -> str:
        """Assemble the final video from scene cards and clip files.

        Parameters
        ----------
        scenes : list[dict]
            Ordered list of scene card dicts from the script generator. Each
            dict must contain at minimum:
            ``{"duration": float, "text_lines": list[str], "emphasis_words": list[str],
               "segment": str}``
        clip_paths : list[str]
            Ordered list of video clip file paths — one per scene.
        nasheed_path : str or None
            Optional path to background nasheed audio. Mixed at 20% volume on
            top of the Sora 2 audio. If ``None`` or file does not exist, it
            is skipped.
        output_filename : str
            Name for the final output file (default ``"final.mp4"``).

        Returns
        -------
        str
            Absolute path to the assembled final MP4.
        """
        if len(scenes) != len(clip_paths):
            raise ValueError(
                f"scenes ({len(scenes)}) and clip_paths ({len(clip_paths)}) must have the same length"
            )

        target_duration = sum(s["duration"] for s in scenes)
        logger.info(
            "Starting assembly — {} scenes, target_duration={:.1f}s, nasheed={}",
            len(scenes),
            target_duration,
            nasheed_path or "none",
        )

        scene_clips: list[Any] = []
        audio_tracks: list[Any] = []
        overlay_clips: list[Any] = []
        final = None

        try:
            scene_start = 0.0

            for idx, (scene, clip_path) in enumerate(zip(scenes, clip_paths)):
                scene_duration = float(scene["duration"])
                logger.debug(
                    "Processing scene {}/{}: duration={:.1f}s, clip={}",
                    idx + 1,
                    len(scenes),
                    scene_duration,
                    Path(clip_path).name,
                )

                # 1. Load, resize, Ken Burns the clip
                clip = self._load_single_clip(clip_path)
                clip = self._resize_clip(clip)
                clip = self._apply_ken_burns(clip)

                # 2. Trim or loop to scene duration
                clip = self._match_duration(clip, scene_duration)

                # 3. Extract clip audio at 80% volume (Sora 2 primary soundscape)
                if clip.audio is not None:
                    scene_audio = clip.audio.with_volume_scaled(self.SORA_AUDIO_VOLUME)
                    # Position audio at the correct timeline offset
                    scene_audio = scene_audio.with_start(scene_start)
                    audio_tracks.append(scene_audio)
                    logger.debug("Extracted audio from scene {} clip", idx + 1)
                else:
                    logger.debug("Scene {} clip has no audio track", idx + 1)

                # Strip audio from clip — audio will be composited separately
                clip = clip.without_audio()

                # 4. Position clip on the timeline
                clip = clip.with_start(scene_start)
                scene_clips.append(clip)

                # 5. Render text overlay cards via Pillow
                text_lines = scene.get("text_lines", [])
                emphasis_words = scene.get("emphasis_words", [])
                segment = scene.get("segment", "")
                font_size = self.TEXT_FONT_SIZE_HOOK if segment == "HOOK" else self.TEXT_FONT_SIZE_NORMAL

                if text_lines:
                    text_overlays = self._create_text_overlays(
                        text_lines=text_lines,
                        emphasis_words=emphasis_words,
                        font_size=font_size,
                        scene_start=scene_start,
                        scene_duration=scene_duration,
                    )
                    overlay_clips.extend(text_overlays)

                scene_start += scene_duration

            if not scene_clips:
                raise ValueError("No valid video clips could be loaded")

            # Concatenate all scene clips
            logger.info("Concatenating {} scene clips", len(scene_clips))
            video = concatenate_videoclips(scene_clips, method="compose")
            logger.info("Concatenated video duration: {:.1f}s", video.duration)

            # Mix Sora 2 audio tracks (each already positioned at correct offset)
            mixed_audio = self._build_audio_mix(audio_tracks, target_duration, nasheed_path)
            if mixed_audio is not None:
                video = video.with_audio(mixed_audio)

            # Composite text overlays onto the video
            if overlay_clips:
                final = CompositeVideoClip([video] + overlay_clips)
                logger.info("Composited {} text overlay clips", len(overlay_clips))
            else:
                final = video

            # Export
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
            all_to_close = scene_clips + overlay_clips + [
                c for c in [mixed_audio, final] if c is not None
            ]
            self._close_clips(all_to_close)

    # ------------------------------------------------------------------
    # Clip loading & resizing
    # ------------------------------------------------------------------

    def _load_single_clip(self, clip_path: str):
        """Load a single video clip file.

        Parameters
        ----------
        clip_path : str
            Path to the video clip file.

        Returns
        -------
        VideoFileClip
            The loaded clip.

        Raises
        ------
        FileNotFoundError
            If the clip file does not exist.
        RuntimeError
            If the clip cannot be loaded.
        """
        p = Path(clip_path)
        if not p.exists():
            raise FileNotFoundError(f"Clip not found: {clip_path}")

        try:
            clip = VideoFileClip(str(p))
            logger.debug(
                "Loaded clip: {} ({:.1f}s, {}x{})",
                p.name,
                clip.duration,
                clip.w,
                clip.h,
            )
            return clip
        except Exception as exc:
            raise RuntimeError(f"Failed to load clip {clip_path}: {exc}") from exc

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
            A video clip.
        target_duration : float
            Desired duration in seconds (from the scene card).

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

    def _build_audio_mix(
        self,
        scene_audio_tracks: list,
        total_duration: float,
        nasheed_path: str | None = None,
    ):
        """Build the final audio mix from per-scene Sora 2 tracks and optional nasheed.

        Parameters
        ----------
        scene_audio_tracks : list
            List of AudioClip objects, each already positioned at the correct
            timeline offset and volume-scaled to 80%.
        total_duration : float
            Total video duration in seconds.
        nasheed_path : str or None
            Optional path to the background nasheed audio file.

        Returns
        -------
        AudioClip or None
            The mixed audio, or None if no audio sources are available.
        """
        tracks = list(scene_audio_tracks)

        if not tracks and not (nasheed_path and Path(nasheed_path).exists()):
            logger.info("No audio sources available — video will be silent")
            return None

        # Optional nasheed at 20% across full duration
        has_nasheed = bool(nasheed_path and Path(nasheed_path).exists())
        if has_nasheed:
            logger.info("Mixing in nasheed: {} at {:.0%} volume", nasheed_path, self.NASHEED_VOLUME)
            nasheed = AudioFileClip(nasheed_path)
            if nasheed.duration < total_duration:
                loops = int(total_duration / nasheed.duration) + 1
                nasheed = concatenate_audioclips([nasheed] * loops)
            nasheed = nasheed.subclipped(0, total_duration)
            nasheed = nasheed.with_volume_scaled(self.NASHEED_VOLUME)
            tracks.append(nasheed)
        elif nasheed_path:
            logger.warning("Nasheed file not found at '{}', skipping", nasheed_path)

        if not tracks:
            return None

        if len(tracks) == 1:
            return tracks[0]

        mixed = CompositeAudioClip(tracks)
        logger.info(
            "Audio mixed — {} Sora 2 scene tracks + {} nasheed track(s)",
            len(scene_audio_tracks),
            1 if has_nasheed else 0,
        )
        return mixed

    # ------------------------------------------------------------------
    # Pillow-based text rendering
    # ------------------------------------------------------------------

    def _render_text_card(
        self,
        text_lines: list[str],
        emphasis_words: list[str],
        width: int,
        height: int,
        font_size: int = 64,
    ) -> np.ndarray:
        """Render a text card as an RGBA numpy array using Pillow.

        Parameters
        ----------
        text_lines : list[str]
            Lines of text to render on this card.
        emphasis_words : list[str]
            Words that should be rendered in gold emphasis colour.
        width : int
            Width of the output image (canvas width).
        height : int
            Height of the output image (canvas height).
        font_size : int
            Font size in pixels (64 for normal, 80 for HOOK segment).

        Returns
        -------
        np.ndarray
            RGBA image array of shape ``(height, width, 4)``.
        """
        img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)

        # Load font
        font = self._load_pil_font(font_size)

        # Reshape Arabic if needed
        rendered_lines = [self._reshape_arabic(line) for line in text_lines]

        # Measure total text block height
        line_height = font_size + 12  # line spacing
        total_text_h = len(rendered_lines) * line_height

        # Background rectangle padding
        pad_x = 40
        pad_y = 24

        # Measure max line width
        max_line_w = 0
        for line in rendered_lines:
            bbox = draw.textbbox((0, 0), line, font=font)
            line_w = bbox[2] - bbox[0]
            if line_w > max_line_w:
                max_line_w = line_w

        bg_w = min(max_line_w + pad_x * 2, width - 40)
        bg_h = total_text_h + pad_y * 2

        bg_x = (width - bg_w) // 2
        bg_y = self.TEXT_Y_CENTER - bg_h // 2

        # Draw rounded rectangle background
        bg_alpha = int(255 * self.TEXT_BG_OPACITY)
        self._draw_rounded_rect(draw, bg_x, bg_y, bg_w, bg_h, self.TEXT_BG_CORNER_RADIUS, bg_alpha)

        # Draw each line of text with word-level emphasis coloring
        emphasis_set = {w.lower().strip(".,!?;:\"'") for w in emphasis_words}
        y_cursor = bg_y + pad_y

        for line in rendered_lines:
            words_in_line = line.split()
            # Measure the whole line to centre it
            bbox_full = draw.textbbox((0, 0), line, font=font)
            line_w = bbox_full[2] - bbox_full[0]
            x_start = (width - line_w) // 2

            # Draw word by word to apply emphasis coloring
            x_cursor = x_start
            for word_idx, word in enumerate(words_in_line):
                word_key = word.lower().strip(".,!?;:\"'")
                color = self.TEXT_EMPHASIS_COLOR if word_key in emphasis_set else self.TEXT_COLOR
                display_word = word if word_idx == len(words_in_line) - 1 else word + " "

                # Draw stroke
                for dx in range(-self.TEXT_STROKE_WIDTH, self.TEXT_STROKE_WIDTH + 1):
                    for dy in range(-self.TEXT_STROKE_WIDTH, self.TEXT_STROKE_WIDTH + 1):
                        if dx == 0 and dy == 0:
                            continue
                        draw.text(
                            (x_cursor + dx, y_cursor + dy),
                            display_word,
                            font=font,
                            fill=(*self.TEXT_STROKE_COLOR, 255),
                        )

                # Draw main text
                draw.text(
                    (x_cursor, y_cursor),
                    display_word,
                    font=font,
                    fill=(*color, 255),
                )

                word_bbox = draw.textbbox((x_cursor, y_cursor), display_word, font=font)
                x_cursor = word_bbox[2]

            y_cursor += line_height

        return np.array(img)

    def _draw_rounded_rect(
        self,
        draw: ImageDraw.ImageDraw,
        x: int,
        y: int,
        w: int,
        h: int,
        radius: int,
        alpha: int,
    ) -> None:
        """Draw a semi-transparent rounded rectangle onto a PIL ImageDraw canvas.

        Parameters
        ----------
        draw : ImageDraw.ImageDraw
            The draw context.
        x, y : int
            Top-left corner coordinates.
        w, h : int
            Width and height of the rectangle.
        radius : int
            Corner radius in pixels.
        alpha : int
            Alpha channel value 0-255.
        """
        fill = (0, 0, 0, alpha)
        r = min(radius, w // 2, h // 2)

        # Main body rectangles
        draw.rectangle([x + r, y, x + w - r, y + h], fill=fill)
        draw.rectangle([x, y + r, x + w, y + h - r], fill=fill)

        # Corners
        draw.ellipse([x, y, x + 2 * r, y + 2 * r], fill=fill)
        draw.ellipse([x + w - 2 * r, y, x + w, y + 2 * r], fill=fill)
        draw.ellipse([x, y + h - 2 * r, x + 2 * r, y + h], fill=fill)
        draw.ellipse([x + w - 2 * r, y + h - 2 * r, x + w, y + h], fill=fill)

    def _create_text_overlays(
        self,
        text_lines: list[str],
        emphasis_words: list[str],
        font_size: int,
        scene_start: float,
        scene_duration: float,
    ) -> list:
        """Create MoviePy ImageClip overlays for all text lines in a scene.

        Lines are shown sequentially within the scene duration. Each card
        fades in over 250ms.

        Parameters
        ----------
        text_lines : list[str]
            Text lines for this scene.
        emphasis_words : list[str]
            Words to render in gold emphasis colour.
        font_size : int
            Font size to use (from scene segment type).
        scene_start : float
            Scene start time on the global timeline (seconds).
        scene_duration : float
            Scene duration in seconds.

        Returns
        -------
        list
            List of MoviePy ImageClip objects (RGBA overlays).
        """
        n = len(text_lines)
        if n == 0:
            return []

        # Distribute duration: each line gets equal share, with a small gap
        gap = 0.5 if n > 1 else 0.0
        available = scene_duration - gap * (n - 1)
        card_duration = max(available / n, 0.5)

        overlays = []

        for i, line in enumerate(text_lines):
            card_start = scene_start + i * (card_duration + gap)
            card_end = card_start + card_duration

            # Don't overflow the scene
            card_end = min(card_end, scene_start + scene_duration - 0.1)
            actual_duration = max(card_end - card_start, 0.3)

            # Render RGBA array via Pillow
            rgba_array = self._render_text_card(
                text_lines=[line],
                emphasis_words=emphasis_words,
                width=self.TARGET_WIDTH,
                height=self.TARGET_HEIGHT,
                font_size=font_size,
            )

            clip = (
                ImageClip(rgba_array, is_mask=False)
                .with_duration(actual_duration)
                .with_start(card_start)
            )

            # Apply fade-in
            fade_dur = min(self.TEXT_FADE_IN_DURATION, actual_duration * 0.3)
            clip = clip.with_effects([FadeIn(fade_dur)])

            overlays.append(clip)
            logger.debug(
                "Text overlay: '{}...' at {:.2f}s for {:.2f}s",
                line[:30],
                card_start,
                actual_duration,
            )

        return overlays

    # ------------------------------------------------------------------
    # Font resolution
    # ------------------------------------------------------------------

    def _resolve_font(self) -> str:
        """Resolve the font file path to use for Pillow text rendering.

        Preference order:
        1. ``assets/fonts/Cormorant-SemiBold.ttf``
        2. Other project-bundled serif fonts
        3. System serif fonts
        4. Arial (fallback)

        Returns
        -------
        str
            Absolute path to the font file, or ``"Arial"`` if not found.
        """
        # Project font candidates (preference order)
        project_candidates = [
            _FONTS_DIR / "Cormorant-SemiBold.ttf",
            _FONTS_DIR / "Cormorant_SemiBold.ttf",
            _FONTS_DIR / "cormorant-semibold.ttf",
            _FONTS_DIR / "Amiri-Regular.ttf",
            _FONTS_DIR / "Amiri.ttf",
            _FONTS_DIR / "amiri-regular.ttf",
        ]
        for font_path in project_candidates:
            if font_path.exists():
                logger.info("Using project font: {}", font_path)
                return str(font_path)

        # System serif fonts
        system_fonts = [
            "/System/Library/Fonts/Supplemental/Georgia.ttf",           # macOS
            "/System/Library/Fonts/Supplemental/Arial.ttf",             # macOS
            "/usr/share/fonts/truetype/freefont/FreeSerif.ttf",         # Linux
            "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",         # Linux
            "/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf",
            "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
            "C:/Windows/Fonts/georgia.ttf",
            "C:/Windows/Fonts/arial.ttf",
        ]
        for font in system_fonts:
            if Path(font).exists():
                logger.info("Using system font: {}", font)
                return font

        logger.warning("No preferred font found — Pillow will use its built-in default")
        return "Arial"

    def _load_pil_font(self, font_size: int) -> ImageFont.FreeTypeFont:
        """Load a Pillow FreeTypeFont at the given size.

        Parameters
        ----------
        font_size : int
            Font size in pixels.

        Returns
        -------
        ImageFont.FreeTypeFont
            The loaded font (or Pillow's built-in default on failure).
        """
        if self.font_path and self.font_path != "Arial":
            try:
                return ImageFont.truetype(self.font_path, font_size)
            except Exception as exc:
                logger.warning("Failed to load font {}: {}", self.font_path, exc)

        # Try common Arial paths as a last resort
        arial_paths = [
            "/System/Library/Fonts/Supplemental/Arial.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
            "C:/Windows/Fonts/arial.ttf",
        ]
        for path in arial_paths:
            if Path(path).exists():
                try:
                    return ImageFont.truetype(path, font_size)
                except Exception:
                    continue

        logger.warning("Could not load any TrueType font — using Pillow built-in default")
        return ImageFont.load_default()

    # ------------------------------------------------------------------
    # Arabic text reshaping
    # ------------------------------------------------------------------

    def _reshape_arabic(self, text: str) -> str:
        """Reshape Arabic text for correct visual rendering.

        Arabic characters need to be reshaped (connected forms) and
        reordered (right-to-left) for correct display in image-based
        text rendering.

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

    # This test requires actual clip files generated by the pipeline.
    sample_scenes = [
        {
            "segment": "HOOK",
            "duration": 5,
            "text_lines": ["Have you ever wondered", "why the Prophet smiled so often?"],
            "emphasis_words": ["Prophet", "smiled"],
        },
        {
            "segment": "BODY",
            "duration": 8,
            "text_lines": ["In a world that constantly tells us to stress,", "he chose joy."],
            "emphasis_words": ["stress", "joy"],
        },
        {
            "segment": "CTA",
            "duration": 5,
            "text_lines": ["Follow for daily reminders."],
            "emphasis_words": [],
        },
    ]
    sample_clips = [
        "output/clips/ai_clip_01.mp4",
        "output/clips/ai_clip_02.mp4",
        "output/clips/ai_clip_03.mp4",
    ]
    sample_nasheed = "assets/nasheed/background.mp3"

    missing = [f for f in sample_clips if not Path(f).exists()]
    if missing:
        logger.error(
            "Cannot run test — missing clip files: {}. "
            "Generate clips first using video_generator.py.",
            missing,
        )
        sys.exit(1)

    assembler = VideoAssembler(output_dir="output")

    logger.info("Starting test assembly...")
    result_path = assembler.assemble(
        scenes=sample_scenes,
        clip_paths=sample_clips,
        nasheed_path=sample_nasheed if Path(sample_nasheed).exists() else None,
        output_filename="test_final.mp4",
    )

    print("\n" + "=" * 60)
    print("ASSEMBLED VIDEO")
    print("=" * 60)
    print(f"Output: {result_path}")
