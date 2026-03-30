"""
Video Generator Module
----------------------
Generates AI video clips via KIE AI (Sora 2 Pro) and fetches stock footage
from Pexels API for the automated Islamic Instagram content pipeline.

Implements a hybrid approach: 2-3 AI-generated hero clips combined with
2-3 stock footage filler/atmospheric clips.
"""

from __future__ import annotations

import json
import random
import time
from datetime import datetime
from pathlib import Path

import requests
from loguru import logger
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)


class VideoGenerator:
    """Generates video clips using KIE AI Sora 2 Pro (AI) and Pexels (stock footage)."""

    # Target dimensions for Instagram Reels (9:16 portrait)
    TARGET_WIDTH = 1080
    TARGET_HEIGHT = 1920

    # KIE API endpoints
    KIE_CREATE_URL = "https://api.kie.ai/api/v1/jobs/createTask"
    KIE_POLL_URL = "https://api.kie.ai/api/v1/jobs/recordInfo"

    # Polling configuration
    POLL_INTERVAL_SECONDS = 20
    DEFAULT_TIMEOUT_SECONDS = 5400  # 90 minutes — Sora 2 Pro can take up to 60+ min

    # Pexels API base URL
    PEXELS_BASE_URL = "https://api.pexels.com/videos/search"

    def __init__(
        self,
        kie_api_key: str,
        pexels_api_key: str,
        output_dir: str = "output",
    ):
        """
        Initialize the video generator.

        Parameters
        ----------
        kie_api_key : str
            API key for KIE AI (Sora 2 Pro access).
        pexels_api_key : str
            API key for Pexels video search.
        output_dir : str
            Directory where generated/downloaded clips will be saved.
        """
        self.kie_api_key = kie_api_key
        self.pexels_api_key = pexels_api_key

        self.output_dir = Path(output_dir)
        self.clips_dir = self.output_dir / "clips"
        self.clips_dir.mkdir(parents=True, exist_ok=True)

        if not kie_api_key:
            logger.warning("KIE_API_KEY is empty — Sora 2 AI clip generation will be unavailable")
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
        """Generate AI video clips via KIE Sora 2 Pro for prompts marked ``type='ai'``.

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
        if not self.kie_api_key:
            logger.warning("KIE API key not set — skipping AI clip generation")
            return []

        ai_prompts = [p for p in visual_prompts if p.get("type") == "ai"]
        if not ai_prompts:
            logger.warning("No AI prompts found in visual_prompts")
            return []

        logger.info("Processing {} AI video prompts via KIE Sora 2 Pro", len(ai_prompts))

        # Phase 1: Submit jobs (or resume from a previous crashed run)
        persisted = self._load_jobs()

        if persisted:
            # Restart scenario: task IDs already submitted, skip re-submission
            jobs = [(j["idx"], j["task_id"], j["output_path"]) for j in persisted]
            logger.info("Resumed {} KIE jobs — skipping re-submission", len(jobs))
        else:
            jobs = []  # list of (idx, task_id, output_path)
            job_records = []  # for persistence
            for idx, prompt in enumerate(ai_prompts, start=1):
                description = prompt.get("description", "")
                # Append audio direction to prompt so Sora 2 generates matching audio
                audio_direction = prompt.get("audio_direction", "")
                if audio_direction:
                    description = f"{description} Audio: {audio_direction}"
                try:
                    duration = int(prompt.get("duration", 8))
                except (TypeError, ValueError):
                    logger.warning("AI prompt #{} has invalid duration '{}', using default 8s", idx, prompt.get("duration"))
                    duration = 8
                if not description:
                    logger.warning("AI prompt #{} has an empty description, skipping", idx)
                    continue
                try:
                    task_id, full_response = self._submit_sora_job(description, duration=duration)
                    logger.info(
                        "KIE job {}/{} submitted — taskId={} | full response: {}",
                        idx, len(ai_prompts), task_id, full_response,
                    )
                    # Validate: do one immediate poll to confirm the job was accepted
                    try:
                        validation_data = self._poll_task(task_id)
                        validation_status = validation_data.get("state", "unknown")
                        if validation_status == "fail":
                            logger.error(
                                "KIE job {}/{} was immediately rejected — taskId={} | validation data: {}",
                                idx, len(ai_prompts), task_id, validation_data,
                            )
                            continue
                        logger.info(
                            "KIE job {}/{} validation OK — status='{}' taskId={}",
                            idx, len(ai_prompts), validation_status, task_id,
                        )
                    except Exception as val_exc:
                        logger.warning(
                            "Could not validate KIE job {}/{} (taskId={}): {} — proceeding anyway",
                            idx, len(ai_prompts), task_id, val_exc,
                        )
                    output_path = str(self.clips_dir / f"ai_clip_{idx:02d}.mp4")
                    jobs.append((idx, task_id, output_path))
                    job_records.append({"idx": idx, "task_id": task_id, "output_path": output_path})
                    # Persist after each submission so a crash mid-loop still saves what we have
                    self._save_jobs(job_records)
                    if idx < len(ai_prompts):
                        time.sleep(2)  # small delay between submissions to avoid rate limiting
                except Exception as exc:
                    logger.error("Failed to submit AI clip {}/{}: {}", idx, len(ai_prompts), exc)
                    continue

        if not jobs:
            logger.warning("No KIE jobs were submitted successfully")
            self._clear_jobs()
            return []

        logger.info("All {} jobs submitted — now polling for completion (timeout={}s)", len(jobs), self.DEFAULT_TIMEOUT_SECONDS)

        # Phase 2: Poll all jobs concurrently
        clip_paths = []
        pending = list(jobs)
        start_time = time.time()
        timeout = self.DEFAULT_TIMEOUT_SECONDS

        while pending and (time.time() - start_time) < timeout:
            still_pending = []
            for idx, task_id, output_path in pending:
                try:
                    data = self._poll_task(task_id)
                    status = data.get("state", "unknown")

                    if status == "success":
                        video_url = self._extract_video_url(data)
                        if video_url:
                            saved = self._download_video(video_url, output_path)
                            clip_paths.append(saved)
                            logger.success("AI clip {} completed and downloaded: {}", idx, saved)
                        else:
                            logger.error("AI clip {} succeeded but no video URL found in response", idx)
                    elif status == "fail":
                        logger.error(
                            "AI clip {} failed — full task data: {}",
                            idx,
                            data,
                        )
                    else:
                        # waiting / queuing / generating
                        progress = data.get("progress")
                        if progress is not None:
                            logger.info("AI clip {} — status='{}' progress={}%", idx, status, progress)
                        else:
                            logger.info("AI clip {} — status='{}'", idx, status)
                        still_pending.append((idx, task_id, output_path))
                except Exception as exc:
                    logger.warning("Error polling AI clip {}: {}", idx, exc)
                    still_pending.append((idx, task_id, output_path))

            pending = still_pending
            if pending:
                logger.info("{} jobs still pending, waiting {}s...", len(pending), self.POLL_INTERVAL_SECONDS)
                time.sleep(self.POLL_INTERVAL_SECONDS)

        if pending:
            logger.error("{} KIE jobs timed out after {}s", len(pending), timeout)

        self._clear_jobs()
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
        return [
            {
                "description": p.get("prompt", ""),
                "type": "stock",  # fallback always uses stock to avoid AI costs
                "duration": p.get("duration_suggestion", 8),
            }
            for p in selected
        ]

    # ------------------------------------------------------------------
    # Job persistence — survives process restarts
    # ------------------------------------------------------------------

    @property
    def _jobs_file(self) -> Path:
        return self.clips_dir / "kie_jobs.json"

    def _save_jobs(self, jobs: list[dict]) -> None:
        """Persist submitted KIE task IDs to disk so restarts can resume polling."""
        payload = {
            "date": datetime.now().strftime("%Y-%m-%d"),
            "jobs": jobs,
        }
        self._jobs_file.write_text(json.dumps(payload, indent=2))

    def _load_jobs(self) -> list[dict] | None:
        """Load persisted jobs if they were submitted today. Returns None if stale/missing."""
        if not self._jobs_file.exists():
            return None
        try:
            payload = json.loads(self._jobs_file.read_text())
            if payload.get("date") != datetime.now().strftime("%Y-%m-%d"):
                logger.info("Stale KIE jobs file ({}), ignoring", payload.get("date"))
                return None
            jobs = payload.get("jobs", [])
            if jobs:
                logger.info("Resuming {} KIE jobs from previous run", len(jobs))
            return jobs
        except Exception as exc:
            logger.warning("Could not read KIE jobs file: {}", exc)
            return None

    def _clear_jobs(self) -> None:
        """Delete the jobs file once all work is complete."""
        if self._jobs_file.exists():
            self._jobs_file.unlink()

    # ------------------------------------------------------------------
    # KIE Sora 2 Pro internals
    # ------------------------------------------------------------------

    def _duration_to_n_frames(self, duration: int) -> str:
        """Map a requested duration in seconds to KIE's n_frames parameter.

        KIE accepts only "10" or "15". Clips <= 10s map to "10", longer to "15".
        """
        return "15" if duration > 10 else "10"

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=5, max=60),
        retry=retry_if_exception_type((requests.RequestException, ConnectionError)),
        before_sleep=lambda rs: logger.warning(
            "Retrying KIE job submission (attempt {}) after: {}",
            rs.attempt_number,
            rs.outcome.exception(),
        ),
    )
    def _submit_sora_job(self, prompt: str, duration: int = 8) -> tuple[str, dict]:
        """Submit a Sora 2 Pro text-to-video job via KIE AI.

        Parameters
        ----------
        prompt : str
            The visual prompt describing the desired video.
        duration : int
            Desired clip duration in seconds (used to select n_frames).

        Returns
        -------
        tuple[str, dict]
            A tuple of (taskId, full_response_data) for logging and validation.
        """
        n_frames = self._duration_to_n_frames(duration)

        headers = {
            "Authorization": f"Bearer {self.kie_api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": "sora-2-text-to-video",
            "input": {
                "prompt": prompt,
                "aspect_ratio": "portrait",
                "n_frames": n_frames,
                "size": "high",
                "remove_watermark": True,
            },
        }

        response = requests.post(
            self.KIE_CREATE_URL, headers=headers, json=payload, timeout=30
        )
        response.raise_for_status()

        data = response.json()
        if data.get("code") != 200:
            logger.error(
                "KIE API unexpected response on submit — full body: {}", data
            )
            raise RuntimeError(
                f"KIE API error on submit: code={data.get('code')} msg={data.get('msg')}"
            )

        task_id = data.get("data", {}).get("taskId")
        if not task_id:
            logger.error("KIE API returned no taskId — full body: {}", data)
            raise ValueError(f"KIE API did not return a taskId. Response: {data}")

        return task_id, data

    def _poll_task(self, task_id: str) -> dict:
        """Fetch the current state of a KIE task.

        Parameters
        ----------
        task_id : str
            The taskId returned by ``_submit_sora_job``.

        Returns
        -------
        dict
            The ``data`` object from the KIE response (contains state, resultJson, etc.).
        """
        headers = {"Authorization": f"Bearer {self.kie_api_key}"}
        response = requests.get(
            self.KIE_POLL_URL,
            headers=headers,
            params={"taskId": task_id},
            timeout=30,
        )
        response.raise_for_status()

        body = response.json()
        if body.get("code") != 200:
            raise RuntimeError(
                f"KIE poll error: code={body.get('code')} msg={body.get('msg')}"
            )

        return body.get("data", {})

    def _extract_video_url(self, task_data: dict) -> str | None:
        """Extract the video download URL from a completed KIE task record.

        The video URL lives inside ``resultJson``, which is a JSON-encoded string
        containing ``{"resultUrls": ["https://...mp4"]}``.

        Parameters
        ----------
        task_data : dict
            The ``data`` object from a KIE poll response.

        Returns
        -------
        str or None
            The video URL, or ``None`` if it could not be found.
        """
        result_json_str = task_data.get("resultJson")
        if not result_json_str:
            return None

        try:
            result = json.loads(result_json_str)
            urls = result.get("resultUrls", [])
            return urls[0] if urls else None
        except (json.JSONDecodeError, IndexError, TypeError) as exc:
            logger.warning("Could not parse resultJson: {} — raw: {}", exc, result_json_str)
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

        logger.info(
            "Pexels returned {} results for '{}'", len(videos), query[:80]
        )
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

    kie_key = os.getenv("KIE_API_KEY")
    pexels_key = os.getenv("PEXELS_API_KEY")

    if not kie_key:
        logger.error("KIE_API_KEY not set in environment or .env file")
        raise SystemExit(1)
    if not pexels_key:
        logger.error("PEXELS_API_KEY not set in environment or .env file")
        raise SystemExit(1)

    generator = VideoGenerator(
        kie_api_key=kie_key,
        pexels_api_key=pexels_key,
        output_dir="output",
    )

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
