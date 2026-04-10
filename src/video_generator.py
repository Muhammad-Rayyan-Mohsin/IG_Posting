"""
Video Generator Module
----------------------
Generates AI video clips via fal.ai (Wan 2.5 preview text-to-video) and
fetches stock footage from Pexels API for the automated Islamic Instagram
content pipeline.

Wan 2.5 generates native audio alongside the video — audio cues embedded in
the prompt (ambient wind, water, birdsong, stone reverb, etc.) become the
primary soundscape of each clip.

Constraints enforced here:
- Each clip is snapped to 5 or 10 seconds (Wan 2.5 enum).
- The sum of clip durations for a single video is capped at 90 seconds.
- Resolution: 720p. Aspect ratio: 9:16 portrait (Instagram Reels).
- Pexels stock footage is used as a per-scene fallback when Wan 2.5 fails.
"""

from __future__ import annotations

import json
import os
import random
from pathlib import Path

import fal_client
import requests
from loguru import logger
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)


class VideoGenerator:
    """Generates video clips using Wan 2.5 (fal.ai) and Pexels (stock footage)."""

    # Target dimensions for Instagram Reels (9:16 portrait)
    TARGET_WIDTH = 1080
    TARGET_HEIGHT = 1920

    # fal.ai model endpoint
    FAL_MODEL = "fal-ai/wan-25-preview/text-to-video"

    # Wan 2.5 request defaults
    WAN_RESOLUTION = "720p"
    WAN_ASPECT_RATIO = "9:16"
    # Compressed to fit Wan 2.5 negative_prompt max of 500 chars.
    WAN_NEGATIVE_PROMPT = (
        "blurry, distorted, low quality, human faces, people, hands, "
        "body parts, silhouettes, watermarks, logos, "
        "English text on surfaces, subtitles burned in scene, "
        "Arabic text, Arabic calligraphy, Arabic script, Quranic text, "
        "Quranic verses, open Quran pages, handwritten Arabic, "
        "calligraphy with legible letters, rendered text on objects"
    )

    # Final video length ceiling (seconds)
    MAX_TOTAL_DURATION = 90

    # Wan 2.5 supports 800-char prompts. Leave 2 chars of safety headroom.
    PROMPT_MAX_CHARS = 798

    # Pexels API base URL
    PEXELS_BASE_URL = "https://api.pexels.com/videos/search"

    def __init__(
        self,
        fal_api_key: str,
        pexels_api_key: str,
        output_dir: str = "output",
    ):
        """
        Initialize the video generator.

        Parameters
        ----------
        fal_api_key : str
            API key for fal.ai. Set as FAL_KEY env var for fal_client.
        pexels_api_key : str
            API key for Pexels video search (fallback).
        output_dir : str
            Directory where generated/downloaded clips will be saved.
        """
        self.fal_api_key = fal_api_key
        self.pexels_api_key = pexels_api_key

        self.output_dir = Path(output_dir)
        self.clips_dir = self.output_dir / "clips"
        self.clips_dir.mkdir(parents=True, exist_ok=True)

        if fal_api_key:
            # fal_client reads FAL_KEY from env, so mirror whatever was passed in.
            os.environ["FAL_KEY"] = fal_api_key
        else:
            logger.warning("FAL_API_KEY is empty — Wan 2.5 AI clip generation will be unavailable")
        if not pexels_api_key:
            logger.warning("PEXELS_API_KEY is empty — Pexels stock footage fallback will be unavailable")

        logger.info(
            "VideoGenerator initialized — model={} resolution={} aspect={} cap={}s",
            self.FAL_MODEL, self.WAN_RESOLUTION, self.WAN_ASPECT_RATIO, self.MAX_TOTAL_DURATION,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def generate_all_clips(
        self,
        scenes: list[dict],
        scene_bible: dict | None = None,
    ) -> list[str]:
        """Generate one Wan 2.5 clip per scene, capped at 90s total.

        Parameters
        ----------
        scenes : list[dict]
            Scene cards from the script generator. Each scene must have
            ``visual_prompt`` and ``audio_direction`` keys; ``duration`` is
            snapped to 5 or 10 seconds.
        scene_bible : dict, optional
            Global style anchors (``film_look``, ``color_anchors``,
            ``ambient_sound_base``) prepended to every prompt so independently
            generated clips feel like one cohesive film.

        Returns
        -------
        list[str]
            Ordered list of local clip file paths. Scenes whose clip could
            not be generated are represented by an empty string so the
            assembler can detect gaps.
        """
        scene_bible = scene_bible or {}
        logger.info("Generating {} Wan 2.5 clips (90s cap)", len(scenes))

        clip_paths: list[str] = []
        total_duration = 0.0

        for idx, scene in enumerate(scenes, start=1):
            snapped_duration = self._snap_duration(scene.get("duration"))

            if total_duration + snapped_duration > self.MAX_TOTAL_DURATION:
                logger.warning(
                    "Scene {}/{} would push total past {}s cap "
                    "(current={}s, requested={}s) — dropping remaining scenes",
                    idx, len(scenes), self.MAX_TOTAL_DURATION,
                    total_duration, snapped_duration,
                )
                clip_paths.extend([""] * (len(scenes) - idx + 1))
                break

            prompt = self._build_prompt(scene, scene_bible)
            if not prompt:
                logger.warning("Scene {}/{} has empty visual_prompt — skipping", idx, len(scenes))
                clip_paths.append("")
                continue

            output_path = str(self.clips_dir / f"scene_clip_{idx:02d}.mp4")

            try:
                clip_path = self._generate_wan25_clip(
                    prompt=prompt,
                    duration_sec=snapped_duration,
                    output_path=output_path,
                    scene_idx=idx,
                )
                clip_paths.append(clip_path)
                total_duration += snapped_duration
                logger.success(
                    "Scene {}/{} clip ready ({:.0f}s) — running total: {:.0f}s / {}s",
                    idx, len(scenes), snapped_duration, total_duration, self.MAX_TOTAL_DURATION,
                )
            except Exception as exc:
                logger.error(
                    "Wan 2.5 failed for scene {}/{}: {} — attempting Pexels fallback",
                    idx, len(scenes), exc,
                )
                fallback = self._pexels_fallback_for_scene(scene, idx)
                if fallback:
                    clip_paths.append(fallback)
                    total_duration += snapped_duration
                else:
                    clip_paths.append("")

        successful = [p for p in clip_paths if p]
        logger.info(
            "Clip generation complete — {}/{} clips produced, total duration {:.0f}s",
            len(successful), len(scenes), total_duration,
        )
        return clip_paths

    def fetch_stock_clips(self, visual_prompts: list[dict]) -> list[str]:
        """Fetch stock footage from Pexels.

        Kept as an emergency fallback path called by ``main.py`` when the
        scene-card pipeline produces zero clips. Each entry uses the
        ``description`` field as the Pexels search query.
        """
        if not self.pexels_api_key:
            logger.warning("Pexels API key not set — skipping stock clip fetching")
            return []

        if not visual_prompts:
            return []

        logger.info("Fetching {} stock clips from Pexels", len(visual_prompts))
        clip_paths: list[str] = []

        for idx, prompt in enumerate(visual_prompts, start=1):
            description = prompt.get("description", "")
            if not description:
                continue

            logger.info(
                "Stock clip {}/{}: searching Pexels for '{}'",
                idx, len(visual_prompts), description[:80],
            )

            try:
                result = self._search_pexels(description, orientation="portrait")
                if not result:
                    continue

                video_url = self._select_best_pexels_file(result)
                if not video_url:
                    continue

                output_path = str(self.clips_dir / f"stock_clip_{idx:02d}.mp4")
                saved_path = self._download_video(video_url, output_path)
                clip_paths.append(saved_path)

                logger.success(
                    "Stock clip {}/{} saved: {}", idx, len(visual_prompts), saved_path,
                )
            except Exception as exc:
                logger.error(
                    "Failed to fetch stock clip {}/{}: {}",
                    idx, len(visual_prompts), exc,
                )
                continue

        return clip_paths

    def get_fallback_prompts(self, count: int = 4, category: str | None = None) -> list[dict]:
        """Load fallback visual prompts from the curated library."""
        config_path = Path(__file__).resolve().parent.parent / "config" / "visual_prompts_library.json"
        if not config_path.exists():
            return [{"description": "peaceful nature landscape with gentle clouds"}]

        with open(config_path) as f:
            library = json.load(f)

        if category:
            library = [p for p in library if p.get("category") == category] or library

        selected = random.sample(library, min(count, len(library)))
        return [{"description": p.get("prompt", "")} for p in selected]

    # ------------------------------------------------------------------
    # Wan 2.5 / fal.ai internals
    # ------------------------------------------------------------------

    @staticmethod
    def _snap_duration(raw_duration) -> int:
        """Snap an arbitrary scene duration to Wan 2.5's enum values (5 or 10)."""
        try:
            value = float(raw_duration)
        except (TypeError, ValueError):
            return 10
        return 5 if value <= 5 else 10

    def _build_prompt(self, scene: dict, scene_bible: dict) -> str:
        """Build a Wan 2.5 prompt from a scene card.

        Structure (labelled sections so the model doesn't confuse visual
        content with voiceover text):

            VISUAL: [film_look]. [scene.visual_prompt].
            AMBIENT SOUND: [scene_bible.ambient_sound_base], [scene.audio_direction].
            VOICEOVER (off-camera audio only, do NOT render this text visually
            in the scene): a warm, reverent English male narrator with an
            unhurried storytelling cadence speaks clearly — <narration>.

        The explicit "do NOT render this text visually" instruction is
        critical: without it, Wan 2.5 sometimes writes the quoted narration
        words onto surfaces in the scene (pages, walls, etc.). Quotes are
        deliberately avoided around the narration text for the same reason.
        """
        film_look = scene_bible.get("film_look", "")
        color_grade = scene_bible.get("color_grade", "")
        ambient_base = scene_bible.get("ambient_sound_base", "")

        visual = (scene.get("visual_prompt") or "").strip()
        audio = (scene.get("audio_direction") or "").strip()
        narration = (scene.get("narration") or "").strip().strip('"').strip("'")

        # --- VISUAL section ---
        visual_parts: list[str] = []
        if film_look and film_look.lower() not in visual.lower():
            visual_parts.append(film_look.strip(". "))
        if color_grade and color_grade.lower() not in visual.lower():
            visual_parts.append(color_grade.strip(". "))
        if visual:
            visual_parts.append(visual.strip(". "))
        visual_str = ". ".join(p for p in visual_parts if p)
        prompt = f"VISUAL: {visual_str}"

        # --- AMBIENT SOUND section (environmental bed only, no voices) ---
        audio_segments: list[str] = []
        if ambient_base and ambient_base.lower() not in audio.lower():
            audio_segments.append(ambient_base.strip(". "))
        if audio:
            audio_segments.append(audio.strip(". "))
        if audio_segments:
            prompt = f"{prompt}. AMBIENT SOUND: {', '.join(audio_segments)}"

        # --- VOICEOVER section (off-camera audio, NOT visual text) ---
        # Prefix is kept short so the full narration fits within PROMPT_MAX_CHARS.
        # The negative_prompt carries the heavy "no text in scene" enforcement.
        if narration:
            prompt = (
                f"{prompt}. VOICEOVER (audio only, do NOT display this text "
                f"in the scene): warm reverent English male narrator speaks "
                f"unhurriedly — {narration}"
            )

        if len(prompt) > self.PROMPT_MAX_CHARS:
            prompt = prompt[: self.PROMPT_MAX_CHARS - 3].rstrip() + "..."
            logger.debug("Truncated prompt to {} chars for Wan 2.5", self.PROMPT_MAX_CHARS)

        return prompt

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=5, max=60),
        retry=retry_if_exception_type((requests.RequestException, ConnectionError, TimeoutError)),
        before_sleep=lambda rs: logger.warning(
            "Retrying Wan 2.5 generation (attempt {}) after: {}",
            rs.attempt_number, rs.outcome.exception(),
        ),
    )
    def _generate_wan25_clip(
        self,
        prompt: str,
        duration_sec: int,
        output_path: str,
        scene_idx: int,
    ) -> str:
        """Submit a single Wan 2.5 text-to-video job and download the result.

        Uses ``fal_client.subscribe`` which blocks until the queue job
        finishes, streaming logs for visibility. Wan 2.5 typically completes
        in 1–3 minutes per clip.
        """
        if not self.fal_api_key:
            raise RuntimeError("FAL_KEY not set — cannot call fal.ai")

        logger.info(
            "Scene {} — submitting Wan 2.5 job ({}s, {}, {})",
            scene_idx, duration_sec, self.WAN_RESOLUTION, self.WAN_ASPECT_RATIO,
        )
        logger.debug("Scene {} prompt ({} chars): {}", scene_idx, len(prompt), prompt)

        def _on_queue_update(update):
            # Stream progress logs from fal.ai for long-running jobs.
            if hasattr(update, "logs") and update.logs:
                for entry in update.logs:
                    msg = entry.get("message") if isinstance(entry, dict) else str(entry)
                    if msg:
                        logger.debug("Scene {} [fal]: {}", scene_idx, msg)

        result = fal_client.subscribe(
            self.FAL_MODEL,
            arguments={
                "prompt": prompt,
                "negative_prompt": self.WAN_NEGATIVE_PROMPT,
                "aspect_ratio": self.WAN_ASPECT_RATIO,
                "resolution": self.WAN_RESOLUTION,
                "duration": str(duration_sec),
                "enable_prompt_expansion": True,
                "enable_safety_checker": True,
            },
            with_logs=True,
            on_queue_update=_on_queue_update,
        )

        video_url = self._extract_video_url(result)
        if not video_url:
            raise RuntimeError(f"Wan 2.5 returned no video URL — raw result: {result}")

        logger.info("Scene {} — Wan 2.5 complete, downloading {}", scene_idx, video_url)
        return self._download_video(video_url, output_path)

    @staticmethod
    def _extract_video_url(result) -> str | None:
        """Pull the MP4 URL out of a fal.ai Wan 2.5 response.

        Expected shape:
            {"video": {"url": "...", "content_type": "video/mp4", ...}, "seed": ...}
        """
        if not isinstance(result, dict):
            return None
        video = result.get("video")
        if isinstance(video, dict):
            return video.get("url")
        if isinstance(video, str):
            return video
        return None

    # ------------------------------------------------------------------
    # Pexels fallback internals
    # ------------------------------------------------------------------

    def _pexels_fallback_for_scene(self, scene: dict, idx: int) -> str | None:
        """Attempt a Pexels stock-clip fallback for a single failed scene."""
        if not self.pexels_api_key:
            logger.warning("Pexels API key not set — cannot run fallback for scene {}", idx)
            return None

        query = scene.get("visual_prompt", "") or ""
        if not query:
            fallback_prompts = self.get_fallback_prompts(count=1)
            query = fallback_prompts[0]["description"] if fallback_prompts else "peaceful nature landscape"

        logger.info("Attempting Pexels fallback for scene {}: '{}'", idx, query[:80])

        try:
            result = self._search_pexels(query, orientation="portrait")
            if not result:
                fallback_prompts = self.get_fallback_prompts(count=1)
                fallback_query = fallback_prompts[0]["description"] if fallback_prompts else "peaceful clouds sunset"
                result = self._search_pexels(fallback_query, orientation="portrait")
            if not result:
                return None

            video_url = self._select_best_pexels_file(result)
            if not video_url:
                return None

            output_path = str(self.clips_dir / f"scene_clip_{idx:02d}_fallback.mp4")
            saved = self._download_video(video_url, output_path)
            logger.info("Pexels fallback for scene {} saved: {}", idx, saved)
            return saved
        except Exception as exc:
            logger.error("Pexels fallback for scene {} failed: {}", idx, exc)
            return None

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=15),
        retry=retry_if_exception_type((requests.RequestException, ConnectionError)),
        before_sleep=lambda rs: logger.warning(
            "Retrying Pexels search (attempt {}) after: {}",
            rs.attempt_number, rs.outcome.exception(),
        ),
    )
    def _search_pexels(self, query: str, orientation: str = "portrait") -> dict | None:
        """Search the Pexels API for videos matching a query."""
        headers = {"Authorization": self.pexels_api_key}
        params = {
            "query": query,
            "orientation": orientation,
            "per_page": 5,
            "size": "medium",
        }

        response = requests.get(
            self.PEXELS_BASE_URL, headers=headers, params=params, timeout=30
        )
        response.raise_for_status()

        data = response.json()
        videos = data.get("videos", [])

        if not videos:
            logger.info("Pexels returned 0 results for '{}'", query[:80])
            return None

        logger.info("Pexels returned {} results for '{}'", len(videos), query[:80])
        return videos[0]

    def _select_best_pexels_file(self, video_result: dict) -> str | None:
        """Select the best video file URL from a Pexels video result.

        Prefers files closest to 1080x1920 portrait dimensions.
        """
        video_files = video_result.get("video_files", [])
        if not video_files:
            return None

        def dimension_score(vf: dict) -> float:
            w = vf.get("width", 0)
            h = vf.get("height", 0)
            if w == 0 or h == 0:
                return float("inf")
            return abs(w - self.TARGET_WIDTH) + abs(h - self.TARGET_HEIGHT)

        video_files_sorted = sorted(video_files, key=dimension_score)
        best = video_files_sorted[0]

        logger.debug(
            "Selected Pexels file: {}x{} ({})",
            best.get("width"), best.get("height"), best.get("quality"),
        )
        return best.get("link")

    # ------------------------------------------------------------------
    # Download helper
    # ------------------------------------------------------------------

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=15),
        retry=retry_if_exception_type((requests.RequestException, ConnectionError)),
        before_sleep=lambda rs: logger.warning(
            "Retrying download (attempt {}) after: {}",
            rs.attempt_number, rs.outcome.exception(),
        ),
    )
    def _download_video(self, url: str, output_path: str) -> str:
        """Download a video file from a URL to a local path."""
        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)

        logger.info("Downloading video to {}", output_path)

        response = requests.get(url, stream=True, timeout=120)
        response.raise_for_status()

        total_bytes = 0
        with open(output, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
                    total_bytes += len(chunk)

        logger.info(
            "Downloaded {:.1f} MB to {}",
            total_bytes / (1024 * 1024), output_path,
        )
        return str(output.resolve())


# ----------------------------------------------------------------------
# CLI test entrypoint
# ----------------------------------------------------------------------

if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()

    fal_key = os.getenv("FAL_API_KEY") or os.getenv("FAL_KEY")
    pexels_key = os.getenv("PEXELS_API_KEY")

    if not fal_key:
        logger.error("FAL_API_KEY not set in environment or .env file")
        raise SystemExit(1)

    generator = VideoGenerator(
        fal_api_key=fal_key,
        pexels_api_key=pexels_key or "",
        output_dir="output",
    )

    sample_scene_bible = {
        "film_look": "35mm Kodak 5219 with natural grain, anamorphic 2.0x, shallow depth of field",
        "color_anchors": ["warm amber", "cream", "deep indigo"],
        "color_grade": "warm Kodak Portra tones with lifted shadows",
        "ambient_sound_base": "gentle desert wind with distant birdsong",
    }
    sample_scenes = [
        {
            "id": 1,
            "duration": 10,
            "narration": (
                "A man once carried a single verse in his heart for forty years "
                "before he understood what it meant."
            ),
            "visual_prompt": (
                "A single brass oil lamp flickering on an ancient stone ledge "
                "inside a vast domed hall, flame dancing and casting warm "
                "shadows that shift across weathered sandstone walls. Dust motes "
                "drift slowly through shafts of golden sunlight piercing narrow "
                "arched windows above. Slow dolly forward, 35mm lens, anamorphic "
                "bokeh. Warm Kodak Portra tones, cinematic film grain, photoreal "
                "4K detail. Light gradually intensifies as camera approaches, "
                "deepening the amber glow across carved stone."
            ),
            "audio_direction": (
                "large stone domed interior with long natural reverb, soft crackle "
                "of oil lamp flame, gentle desert wind outside, distant call to "
                "prayer echoing faintly through the arches"
            ),
        },
    ]

    logger.info("Starting Wan 2.5 test clip generation...")
    result = generator.generate_all_clips(sample_scenes, sample_scene_bible)

    print("\n" + "=" * 60)
    print("GENERATED CLIPS")
    print("=" * 60)
    print(json.dumps(result, indent=2))
