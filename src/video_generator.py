"""
Video Generator Module
----------------------
Generates AI video clips via Sora 2 (OpenAI API) and fetches stock footage
from Pexels API for the automated Islamic Instagram content pipeline.

Implements a hybrid approach: 2-3 AI-generated hero clips combined with
2-3 stock footage filler/atmospheric clips.
"""

import json
import random
import time
from pathlib import Path

import requests
from loguru import logger
from openai import APIConnectionError, APIError, OpenAI, RateLimitError
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)


class VideoGenerator:
    """Generates video clips using Sora 2 (AI) and Pexels (stock footage)."""

    # Target dimensions for Instagram Reels (9:16 portrait)
    TARGET_WIDTH = 1080
    TARGET_HEIGHT = 1920

    # Sora 2 polling configuration
    POLL_INTERVAL_SECONDS = 10
    DEFAULT_TIMEOUT_SECONDS = 600  # 10 minutes

    # Valid Sora 2 durations (seconds) — the API only accepts these values
    VALID_DURATIONS = [4, 8, 12, 16, 20]

    # Pexels API base URL
    PEXELS_BASE_URL = "https://api.pexels.com/videos/search"

    def __init__(
        self,
        openai_api_key: str,
        pexels_api_key: str,
        output_dir: str = "output",
    ):
        """
        Initialize the video generator.

        Parameters
        ----------
        openai_api_key : str
            API key for OpenAI (Sora 2 access).
        pexels_api_key : str
            API key for Pexels video search.
        output_dir : str
            Directory where generated/downloaded clips will be saved.
        """
        self.client = OpenAI(api_key=openai_api_key)
        self.pexels_api_key = pexels_api_key

        self.output_dir = Path(output_dir)
        self.clips_dir = self.output_dir / "clips"
        self.clips_dir.mkdir(parents=True, exist_ok=True)

        if not openai_api_key:
            logger.warning("OPENAI_API_KEY is empty — Sora 2 AI clip generation will be unavailable")
        if not pexels_api_key:
            logger.warning("PEXELS_API_KEY is empty — Pexels stock footage fetching will be unavailable")

        logger.info("VideoGenerator initialized — output dir: {}", self.output_dir)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def generate_all_clips(self, visual_prompts: list[dict]) -> dict:
        """Generate both AI and stock clips from a list of visual prompts.

        Each prompt dict is expected to have at least:
            - ``type``: ``"ai"`` or ``"stock"``
            - ``description``: the textual prompt / search query
            - ``duration``: desired clip length in seconds (optional, default 10)

        Parameters
        ----------
        visual_prompts : list[dict]
            Visual prompts produced by the script generator.

        Returns
        -------
        dict
            ``{"ai_clips": [...], "stock_clips": [...]}`` with file paths.
        """
        logger.info(
            "Generating clips from {} visual prompts", len(visual_prompts)
        )

        ai_clips = self.generate_ai_clips(visual_prompts)
        stock_clips = self.fetch_stock_clips(visual_prompts)

        logger.info(
            "All clips ready — {} AI clips, {} stock clips",
            len(ai_clips),
            len(stock_clips),
        )
        return {"ai_clips": ai_clips, "stock_clips": stock_clips}

    def generate_ai_clips(self, visual_prompts: list[dict]) -> list[str]:
        """Generate AI video clips via Sora 2 for prompts marked ``type='ai'``.

        Parameters
        ----------
        visual_prompts : list[dict]
            Full list of visual prompts; only those with ``type == "ai"``
            will be processed.

        Returns
        -------
        list[str]
            List of local file paths to the downloaded AI-generated clips.
        """
        if not self.client.api_key:
            logger.warning("OpenAI API key not set — skipping AI clip generation")
            return []

        ai_prompts = [p for p in visual_prompts if p.get("type") == "ai"]
        if not ai_prompts:
            logger.warning("No AI prompts found in visual_prompts")
            return []

        logger.info("Processing {} AI video prompts via Sora 2", len(ai_prompts))

        # Phase 1: Submit all jobs
        jobs = []  # list of (idx, generation_id, output_path)
        for idx, prompt in enumerate(ai_prompts, start=1):
            description = prompt.get("description", "")
            duration = prompt.get("duration", 8)
            if not description:
                logger.warning("AI prompt #{} has an empty description, skipping", idx)
                continue
            try:
                generation_id = self._submit_sora_job(description, duration=duration)
                output_path = str(self.clips_dir / f"ai_clip_{idx:02d}.mp4")
                jobs.append((idx, generation_id, output_path))
                logger.info("Sora 2 job {}/{} submitted — id={}", idx, len(ai_prompts), generation_id)
            except Exception as exc:
                logger.error("Failed to submit AI clip {}/{}: {}", idx, len(ai_prompts), exc)
                continue

        if not jobs:
            logger.warning("No Sora 2 jobs were submitted successfully")
            return []

        logger.info("All {} jobs submitted — now polling for completion", len(jobs))

        # Phase 2: Poll all jobs concurrently
        clip_paths = []
        pending = list(jobs)  # copy
        start_time = time.time()
        timeout = self.DEFAULT_TIMEOUT_SECONDS

        while pending and (time.time() - start_time) < timeout:
            still_pending = []
            for idx, gen_id, output_path in pending:
                try:
                    response = self.client.responses.retrieve(gen_id)
                    status = self._extract_job_status(response)

                    if status == "completed":
                        video_url = self._extract_video_url(response)
                        if video_url:
                            saved = self._download_video(video_url, output_path)
                            clip_paths.append(saved)
                            logger.success("AI clip {} completed and downloaded: {}", idx, saved)
                        else:
                            logger.error("AI clip {} completed but no video URL found", idx)
                    elif status in ("failed", "cancelled", "expired"):
                        logger.error("AI clip {} ended with status '{}'", idx, status)
                    else:
                        still_pending.append((idx, gen_id, output_path))
                except Exception as exc:
                    logger.warning("Error polling AI clip {}: {}", idx, exc)
                    still_pending.append((idx, gen_id, output_path))

            pending = still_pending
            if pending:
                logger.debug("{} jobs still pending, waiting {}s...", len(pending), self.POLL_INTERVAL_SECONDS)
                time.sleep(self.POLL_INTERVAL_SECONDS)

        if pending:
            logger.error("{} Sora 2 jobs timed out after {}s", len(pending), timeout)

        return clip_paths

    def fetch_stock_clips(self, visual_prompts: list[dict]) -> list[str]:
        """Fetch stock footage from Pexels for prompts marked ``type='stock'``.

        Parameters
        ----------
        visual_prompts : list[dict]
            Full list of visual prompts; only those with ``type == "stock"``
            will be processed.

        Returns
        -------
        list[str]
            List of local file paths to the downloaded stock clips.
        """
        if not self.pexels_api_key:
            logger.warning("Pexels API key not set — skipping stock clip fetching")
            return []

        stock_prompts = [p for p in visual_prompts if p.get("type") == "stock"]
        if not stock_prompts:
            logger.warning("No stock prompts found in visual_prompts")
            return []

        logger.info("Fetching {} stock clips from Pexels", len(stock_prompts))
        clip_paths: list[str] = []

        for idx, prompt in enumerate(stock_prompts, start=1):
            description = prompt.get("description", "")
            if not description:
                logger.warning("Stock prompt #{} has an empty description, skipping", idx)
                continue

            logger.info(
                "Stock clip {}/{}: searching Pexels for '{}'",
                idx,
                len(stock_prompts),
                description[:80],
            )

            try:
                result = self._search_pexels(description, orientation="portrait")
                if not result:
                    logger.warning(
                        "No Pexels results for '{}', skipping", description[:80]
                    )
                    continue

                video_url = self._select_best_pexels_file(result)
                if not video_url:
                    logger.warning(
                        "No suitable video file found in Pexels result, skipping"
                    )
                    continue

                output_path = str(self.clips_dir / f"stock_clip_{idx:02d}.mp4")
                saved_path = self._download_video(video_url, output_path)
                clip_paths.append(saved_path)

                logger.success(
                    "Stock clip {}/{} saved: {}", idx, len(stock_prompts), saved_path
                )

            except Exception as exc:
                logger.error(
                    "Failed to fetch stock clip {}/{}: {}", idx, len(stock_prompts), exc
                )
                continue

        return clip_paths

    def get_fallback_prompts(self, count: int = 4, category: str = None) -> list[dict]:
        """Load fallback visual prompts from the curated library."""
        config_path = Path(__file__).resolve().parent.parent / "config" / "visual_prompts_library.json"
        if not config_path.exists():
            return [{"description": "peaceful nature landscape with gentle clouds", "type": "stock"}]

        with open(config_path) as f:
            library = json.load(f)

        if category:
            library = [p for p in library if p.get("category") == category] or library

        selected = random.sample(library, min(count, len(library)))
        # Convert library format to visual_prompt format expected by fetch methods
        return [
            {
                "description": p.get("prompt", ""),
                "type": "stock",  # fallback always uses stock to avoid Sora costs
                "duration": p.get("duration_suggestion", 8),
            }
            for p in selected
        ]

    # ------------------------------------------------------------------
    # Sora 2 internals
    # ------------------------------------------------------------------

    def _snap_duration(self, requested: int) -> int:
        """Snap a requested duration to the nearest valid Sora 2 duration."""
        return min(self.VALID_DURATIONS, key=lambda d: abs(d - requested))

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=5, max=60),
        retry=retry_if_exception_type((APIError, APIConnectionError, RateLimitError, ConnectionError)),
        before_sleep=lambda rs: logger.warning(
            "Retrying Sora 2 submission (attempt {}) after: {}",
            rs.attempt_number,
            rs.outcome.exception(),
        ),
    )
    def _submit_sora_job(self, prompt: str, duration: int = 8) -> str:
        """Submit a Sora 2 video generation job.

        NOTE: The Sora 2 API is accessed via the OpenAI Python SDK. The exact
        endpoint and response shape may evolve — this method isolates the
        submission call so it can be updated in one place.

        Parameters
        ----------
        prompt : str
            The visual prompt describing the desired video.
        duration : int
            Desired clip duration in seconds (default 8).

        Returns
        -------
        str
            The generation/job ID used for polling.
        """
        # Snap the requested duration to a valid Sora 2 value (4, 8, 12, 16, or 20).
        # The official Sora 2 guide recommends shorter clips (4-8s) for higher
        # quality output; the pipeline stitches clips together as needed.
        duration = self._snap_duration(duration)

        # ----- Sora 2 API call -----
        # The OpenAI Sora API uses the responses endpoint with a
        # video_generation tool. Adjust this block if the API surface changes.
        response = self.client.responses.create(
            model="sora",
            input=prompt,
            tools=[
                {
                    "type": "video_generation",
                    "size": f"{self.TARGET_WIDTH}x{self.TARGET_HEIGHT}",
                    "duration": duration,
                }
            ],
        )

        # Extract the generation ID from the response.
        # The response contains output items; find the one with video generation
        # results and pull its ID.
        generation_id = self._extract_generation_id(response)
        if not generation_id:
            raise ValueError(
                "Sora 2 response did not contain a generation ID. "
                f"Response: {response}"
            )

        return generation_id

    def _extract_generation_id(self, response) -> str | None:
        """Extract the generation/job ID from a Sora 2 submission response.

        This helper exists so that if the OpenAI response shape changes,
        only this method needs updating.

        Parameters
        ----------
        response
            The response object from ``client.responses.create()``.

        Returns
        -------
        str or None
            The generation ID, or ``None`` if it could not be found.
        """
        # Try common response structures:

        # 1) response.id at the top level
        if hasattr(response, "id") and response.id:
            return response.id

        # 2) response.output containing items with an id
        if hasattr(response, "output") and response.output:
            for item in response.output:
                if hasattr(item, "id") and item.id:
                    return item.id
                # Nested generation_id field
                if hasattr(item, "generation_id"):
                    return item.generation_id

        # 3) dict-style access (if the SDK returns raw dicts)
        if isinstance(response, dict):
            if "id" in response:
                return response["id"]
            for item in response.get("output", []):
                if "id" in item:
                    return item["id"]

        return None

    def _poll_sora_job(
        self,
        generation_id: str,
        timeout: int | None = None,
    ) -> str:
        """Poll a Sora 2 generation job until completion.

        Parameters
        ----------
        generation_id : str
            The job/generation ID returned by ``_submit_sora_job``.
        timeout : int, optional
            Maximum seconds to wait before raising a ``TimeoutError``.
            Defaults to ``DEFAULT_TIMEOUT_SECONDS`` (600 s / 10 min).

        Returns
        -------
        str
            The URL of the generated video.

        Raises
        ------
        TimeoutError
            If the job does not complete within the timeout window.
        RuntimeError
            If the job fails or is cancelled.
        """
        timeout = timeout or self.DEFAULT_TIMEOUT_SECONDS
        start_time = time.time()

        logger.info(
            "Polling Sora 2 job {} (timeout={}s, interval={}s)",
            generation_id,
            timeout,
            self.POLL_INTERVAL_SECONDS,
        )

        while True:
            elapsed = time.time() - start_time
            if elapsed > timeout:
                raise TimeoutError(
                    f"Sora 2 job {generation_id} did not complete within "
                    f"{timeout} seconds"
                )

            # ----- Poll the job status -----
            # Adjust this call if the OpenAI SDK polling API changes.
            try:
                response = self.client.responses.retrieve(generation_id)
            except Exception as exc:
                logger.warning(
                    "Error polling Sora 2 job {} (elapsed {:.0f}s): {}",
                    generation_id,
                    elapsed,
                    exc,
                )
                time.sleep(self.POLL_INTERVAL_SECONDS)
                continue

            # Determine status from the response
            status = self._extract_job_status(response)

            if status == "completed":
                video_url = self._extract_video_url(response)
                if video_url:
                    logger.info(
                        "Sora 2 job {} completed after {:.0f}s",
                        generation_id,
                        elapsed,
                    )
                    return video_url
                raise RuntimeError(
                    f"Sora 2 job {generation_id} completed but no video URL found"
                )

            if status in ("failed", "cancelled", "expired"):
                raise RuntimeError(
                    f"Sora 2 job {generation_id} ended with status '{status}'"
                )

            logger.debug(
                "Sora 2 job {} — status='{}', elapsed={:.0f}s",
                generation_id,
                status,
                elapsed,
            )
            time.sleep(self.POLL_INTERVAL_SECONDS)

    def _extract_job_status(self, response) -> str:
        """Extract the status string from a Sora 2 poll response.

        Parameters
        ----------
        response
            The response from ``client.responses.retrieve()``.

        Returns
        -------
        str
            One of ``"completed"``, ``"in_progress"``, ``"failed"``,
            ``"cancelled"``, ``"expired"``, or ``"unknown"``.
        """
        # Object attribute access
        if hasattr(response, "status"):
            return str(response.status).lower()

        # Dict access
        if isinstance(response, dict) and "status" in response:
            return str(response["status"]).lower()

        return "unknown"

    def _extract_video_url(self, response) -> str | None:
        """Extract the video download URL from a completed Sora 2 response.

        Parameters
        ----------
        response
            The completed response object.

        Returns
        -------
        str or None
            The video URL, or ``None`` if it could not be found.
        """
        # Try output items first
        if hasattr(response, "output") and response.output:
            for item in response.output:
                # Direct url attribute
                if hasattr(item, "url") and item.url:
                    return item.url
                # Nested video/result with url
                if hasattr(item, "video") and hasattr(item.video, "url"):
                    return item.video.url
                if hasattr(item, "result") and hasattr(item.result, "url"):
                    return item.result.url
                # content list pattern
                if hasattr(item, "content") and item.content:
                    for content_item in item.content:
                        if hasattr(content_item, "url") and content_item.url:
                            return content_item.url

        # Dict fallback
        if isinstance(response, dict):
            for item in response.get("output", []):
                if "url" in item:
                    return item["url"]

        return None

    # ------------------------------------------------------------------
    # Pexels internals
    # ------------------------------------------------------------------

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=15),
        retry=retry_if_exception_type((requests.RequestException, ConnectionError)),
        before_sleep=lambda rs: logger.warning(
            "Retrying Pexels search (attempt {}) after: {}",
            rs.attempt_number,
            rs.outcome.exception(),
        ),
    )
    def _search_pexels(
        self, query: str, orientation: str = "portrait"
    ) -> dict | None:
        """Search the Pexels API for videos matching a query.

        Parameters
        ----------
        query : str
            Search keywords (e.g. ``"sunset over mosque"``).
        orientation : str
            Video orientation — ``"portrait"``, ``"landscape"``, or
            ``"square"``. Defaults to ``"portrait"`` for Instagram Reels.

        Returns
        -------
        dict or None
            The best-matching video object from Pexels, or ``None`` if no
            results were found.
        """
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

        logger.info(
            "Pexels returned {} results for '{}'", len(videos), query[:80]
        )

        # Return the first (most relevant) result
        return videos[0]

    def _select_best_pexels_file(self, video_result: dict) -> str | None:
        """Select the best video file URL from a Pexels video result.

        Prefers files closest to 1080x1920 portrait dimensions. Falls back
        to the highest-resolution available file.

        Parameters
        ----------
        video_result : dict
            A single video object from the Pexels API response.

        Returns
        -------
        str or None
            The download URL for the chosen video file, or ``None`` if no
            video files are present.
        """
        video_files = video_result.get("video_files", [])
        if not video_files:
            return None

        # Score each file by how close its dimensions are to our target
        def dimension_score(vf: dict) -> float:
            w = vf.get("width", 0)
            h = vf.get("height", 0)
            if w == 0 or h == 0:
                return float("inf")
            return abs(w - self.TARGET_WIDTH) + abs(h - self.TARGET_HEIGHT)

        # Sort by closeness to target, then by resolution (higher is better)
        video_files_sorted = sorted(video_files, key=dimension_score)
        best = video_files_sorted[0]

        logger.debug(
            "Selected Pexels file: {}x{} ({})",
            best.get("width"),
            best.get("height"),
            best.get("quality"),
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
            rs.attempt_number,
            rs.outcome.exception(),
        ),
    )
    def _download_video(self, url: str, output_path: str) -> str:
        """Download a video file from a URL to a local path.

        Uses streaming to handle large files efficiently.

        Parameters
        ----------
        url : str
            The video download URL.
        output_path : str
            Local destination path for the downloaded file.

        Returns
        -------
        str
            The absolute path to the saved file.
        """
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
            total_bytes / (1024 * 1024),
            output_path,
        )

        return str(output.resolve())


# ----------------------------------------------------------------------
# CLI test entrypoint
# ----------------------------------------------------------------------

if __name__ == "__main__":
    import os

    from dotenv import load_dotenv

    load_dotenv()

    openai_key = os.getenv("OPENAI_API_KEY")
    pexels_key = os.getenv("PEXELS_API_KEY")

    if not openai_key:
        logger.error("OPENAI_API_KEY not set in environment or .env file")
        raise SystemExit(1)
    if not pexels_key:
        logger.error("PEXELS_API_KEY not set in environment or .env file")
        raise SystemExit(1)

    generator = VideoGenerator(
        openai_api_key=openai_key,
        pexels_api_key=pexels_key,
        output_dir="output",
    )

    # Sample visual prompts (as the script generator would produce)
    sample_prompts = [
        {
            "type": "ai",
            "description": (
                "A serene mosque at sunset with golden light streaming through "
                "arched windows, dust particles floating in the air, "
                "cinematic 4K, slow motion"
            ),
            "duration": 8,
        },
        {
            "type": "ai",
            "description": (
                "Close-up of an ornate Quran on a wooden stand, pages gently "
                "turning in a soft breeze, warm candlelight, bokeh background"
            ),
            "duration": 6,
        },
        {
            "type": "stock",
            "description": "peaceful nature sunset clouds timelapse",
            "duration": 5,
        },
        {
            "type": "stock",
            "description": "person praying meditation peaceful",
            "duration": 5,
        },
    ]

    logger.info("Starting test clip generation...")
    result = generator.generate_all_clips(sample_prompts)

    print("\n" + "=" * 60)
    print("GENERATED CLIPS")
    print("=" * 60)
    print(json.dumps(result, indent=2))
