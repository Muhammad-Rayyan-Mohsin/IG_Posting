"""
Instagram Poster Module
-----------------------
Posts videos as Reels to Instagram using the Instagram Graph API (official).
Handles uploading to Cloudflare R2 (S3-compatible) for public URL hosting.

Pipeline:
    1. Upload video to Cloudflare R2 -> get a publicly accessible URL
    2. Create an Instagram Reel media container with the video URL and caption
    3. Poll the container status until it is ready (FINISHED)
    4. Publish the Reel
"""

import time
import uuid
from datetime import datetime
from pathlib import Path

import boto3
import requests
from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

# Instagram Graph API base URL
IG_API_BASE = "https://graph.instagram.com/v21.0"


class InstagramPoster:
    """Uploads videos to Cloudflare R2 and publishes them as Instagram Reels
    via the Instagram Graph API."""

    def __init__(
        self,
        ig_user_id: str,
        ig_access_token: str,
        r2_account_id: str,
        r2_access_key: str,
        r2_secret_key: str,
        r2_bucket_name: str,
        r2_public_url: str,
    ):
        """
        Parameters
        ----------
        ig_user_id : str
            Instagram Business account user ID.
        ig_access_token : str
            Long-lived Instagram Graph API access token.
        r2_account_id : str
            Cloudflare account ID.
        r2_access_key : str
            Cloudflare R2 access key ID.
        r2_secret_key : str
            Cloudflare R2 secret access key.
        r2_bucket_name : str
            Name of the R2 bucket used for video hosting.
        r2_public_url : str
            Public URL base for the R2 bucket
            (e.g. ``https://pub-xxx.r2.dev``).
        """
        # Instagram credentials
        self.ig_user_id = ig_user_id
        self.ig_access_token = ig_access_token

        # R2 / S3 configuration
        self.r2_bucket_name = r2_bucket_name
        self.r2_public_url = r2_public_url.rstrip("/")

        # Set up boto3 S3 client for Cloudflare R2
        endpoint_url = f"https://{r2_account_id}.r2.cloudflarestorage.com"
        self.s3_client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=r2_access_key,
            aws_secret_access_key=r2_secret_key,
            region_name="auto",
        )
        logger.info(
            "InstagramPoster initialized — IG user {}, R2 bucket '{}'",
            ig_user_id,
            r2_bucket_name,
        )

    # ------------------------------------------------------------------
    # Instagram token validation
    # ------------------------------------------------------------------

    def validate_token(self) -> None:
        """Validate the Instagram access token by calling the /me endpoint.

        Raises
        ------
        RuntimeError
            If the token is invalid or the API returns an error.
        """
        url = f"{IG_API_BASE}/me"
        params = {
            "fields": "id",
            "access_token": self.ig_access_token,
        }
        logger.info("Validating Instagram access token...")
        try:
            response = requests.get(url, params=params, timeout=15)
            response.raise_for_status()
            data = response.json()
        except requests.RequestException as exc:
            raise RuntimeError(
                f"Instagram token validation request failed: {exc}"
            ) from exc

        if "error" in data:
            error_msg = data["error"].get("message", "Unknown error")
            raise RuntimeError(
                f"Instagram access token is invalid or expired: {error_msg}"
            )

        logger.info(
            "Instagram token valid — account ID: {}", data.get("id", "unknown")
        )

    # ------------------------------------------------------------------
    # Cloudflare R2 upload
    # ------------------------------------------------------------------

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=2, max=30),
        retry=retry_if_exception_type(Exception),
        reraise=True,
    )
    def upload_to_r2(self, video_path: str) -> str:
        """Upload a video file to Cloudflare R2 and return its public URL.

        A unique key is generated using a timestamp and UUID to avoid
        collisions.

        Parameters
        ----------
        video_path : str
            Local path to the video file (MP4).

        Returns
        -------
        str
            The public URL of the uploaded video.
        """
        video_file = Path(video_path)
        if not video_file.exists():
            raise FileNotFoundError(f"Video file not found: {video_path}")

        # Generate a unique object key
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        unique_id = uuid.uuid4().hex[:8]
        key = f"reels/{timestamp}_{unique_id}{video_file.suffix}"

        logger.info("Uploading {} to R2 as '{}'", video_file.name, key)

        self.s3_client.upload_file(
            str(video_file),
            self.r2_bucket_name,
            key,
            ExtraArgs={"ContentType": "video/mp4"},
        )

        public_url = f"{self.r2_public_url}/{key}"
        logger.info("Upload complete — public URL: {}", public_url)
        return public_url

    # ------------------------------------------------------------------
    # Instagram Graph API — media container
    # ------------------------------------------------------------------

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=2, max=30),
        retry=retry_if_exception_type((requests.RequestException,)),
        reraise=True,
    )
    def create_media_container(self, video_url: str, caption: str) -> str:
        """Create an Instagram Reel media container.

        Parameters
        ----------
        video_url : str
            Publicly accessible URL of the video file.
        caption : str
            Caption text for the Reel (may include hashtags).

        Returns
        -------
        str
            The container ID returned by the Instagram API.

        Raises
        ------
        requests.RequestException
            On network errors (retried automatically).
        RuntimeError
            If the API returns an error response.
        """
        url = f"{IG_API_BASE}/{self.ig_user_id}/media"
        params = {
            "video_url": video_url,
            "caption": caption,
            "media_type": "REELS",
            "access_token": self.ig_access_token,
        }

        logger.info("Creating media container for Reel...")
        response = requests.post(url, params=params, timeout=60)
        response.raise_for_status()

        data = response.json()
        if "id" not in data:
            error_msg = data.get("error", {}).get("message", "Unknown error")
            raise RuntimeError(
                f"Failed to create media container: {error_msg}"
            )

        container_id = data["id"]
        logger.info("Media container created — ID: {}", container_id)
        return container_id

    # ------------------------------------------------------------------
    # Instagram Graph API — poll container status
    # ------------------------------------------------------------------

    def wait_for_container(self, container_id: str, timeout: int = 300) -> bool:
        """Poll the media container status until it is ready.

        Parameters
        ----------
        container_id : str
            The container ID to poll.
        timeout : int
            Maximum number of seconds to wait before giving up. Default 300.

        Returns
        -------
        bool
            ``True`` if the container reached ``FINISHED`` status,
            ``False`` if it errored out or timed out.
        """
        url = f"{IG_API_BASE}/{container_id}"
        params = {
            "fields": "status_code",
            "access_token": self.ig_access_token,
        }

        poll_interval = 10  # seconds between polls
        elapsed = 0

        logger.info(
            "Waiting for container {} to finish (timeout={}s)...",
            container_id,
            timeout,
        )

        while elapsed < timeout:
            try:
                response = requests.get(url, params=params, timeout=30)
                response.raise_for_status()
                data = response.json()
                status = data.get("status_code", "UNKNOWN")
            except requests.RequestException as exc:
                logger.warning("Error polling container status: {}", exc)
                status = "POLL_ERROR"

            logger.debug("Container {} status: {} ({}s elapsed)", container_id, status, elapsed)

            if status == "FINISHED":
                logger.info("Container {} is ready", container_id)
                return True

            if status == "ERROR":
                logger.error(
                    "Container {} processing failed — response: {}",
                    container_id,
                    data,
                )
                return False

            time.sleep(poll_interval)
            elapsed += poll_interval

        logger.error(
            "Timed out waiting for container {} after {}s", container_id, timeout
        )
        return False

    # ------------------------------------------------------------------
    # Instagram Graph API — publish
    # ------------------------------------------------------------------

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=2, max=30),
        retry=retry_if_exception_type((requests.RequestException,)),
        reraise=True,
    )
    def publish(self, container_id: str) -> str:
        """Publish a finished media container as an Instagram Reel.

        Parameters
        ----------
        container_id : str
            The container ID of the prepared Reel.

        Returns
        -------
        str
            The Instagram post ID of the published Reel.

        Raises
        ------
        requests.RequestException
            On network errors (retried automatically).
        RuntimeError
            If the API returns an error response.
        """
        url = f"{IG_API_BASE}/{self.ig_user_id}/media_publish"
        params = {
            "creation_id": container_id,
            "access_token": self.ig_access_token,
        }

        logger.info("Publishing container {}...", container_id)
        response = requests.post(url, params=params, timeout=60)
        response.raise_for_status()

        data = response.json()
        if "id" not in data:
            error_msg = data.get("error", {}).get("message", "Unknown error")
            raise RuntimeError(f"Failed to publish Reel: {error_msg}")

        post_id = data["id"]
        logger.info("Reel published successfully — Post ID: {}", post_id)
        return post_id

    # ------------------------------------------------------------------
    # Full pipeline
    # ------------------------------------------------------------------

    def post_reel(self, video_path: str, caption: str) -> dict:
        """Execute the full posting pipeline.

        Steps:
            1. Upload video to Cloudflare R2
            2. Create an Instagram media container
            3. Wait for the container to finish processing
            4. Publish the Reel

        Parameters
        ----------
        video_path : str
            Local path to the final video file (MP4).
        caption : str
            Caption for the Reel (including hashtags).

        Returns
        -------
        dict
            Result dict with keys:
            - ``video_url``: Public R2 URL of the uploaded video.
            - ``container_id``: Instagram media container ID.
            - ``post_id``: Instagram post ID (if published).
            - ``status``: ``"posted"`` on success, ``"failed"`` on failure.
        """
        result = {
            "video_url": None,
            "container_id": None,
            "post_id": None,
            "permalink": None,
            "status": "failed",
        }

        # Pre-flight: validate the Instagram token before spending time on R2 upload
        self.validate_token()

        # Step 1: Upload to R2
        logger.info("Step 1/4 — Uploading video to Cloudflare R2")
        video_url = self.upload_to_r2(video_path)
        result["video_url"] = video_url

        # Step 2: Create media container
        logger.info("Step 2/4 — Creating Instagram media container")
        container_id = self.create_media_container(video_url, caption)
        result["container_id"] = container_id

        # Step 3: Wait for container to be ready
        logger.info("Step 3/4 — Waiting for container to finish processing")
        ready = self.wait_for_container(container_id)
        if not ready:
            logger.error("Container failed to process — aborting publish")
            return result

        # Step 4: Publish
        logger.info("Step 4/4 — Publishing Reel")
        post_id = self.publish(container_id)
        result["post_id"] = post_id
        result["status"] = "posted"

        # Fetch the permalink for the published Reel
        try:
            permalink_url = (
                f"{IG_API_BASE}/{post_id}"
                f"?fields=permalink&access_token={self.ig_access_token}"
            )
            permalink_resp = requests.get(permalink_url, timeout=15)
            permalink_resp.raise_for_status()
            permalink_data = permalink_resp.json()
            permalink = permalink_data.get("permalink", "")
            result["permalink"] = permalink
            if permalink:
                logger.info("Reel permalink: {}", permalink)
        except Exception as exc:
            logger.warning("Could not fetch Reel permalink: {}", exc)

        logger.info(
            "Reel posted successfully — Post ID: {}, Video URL: {}",
            post_id,
            video_url,
        )
        return result


# ----------------------------------------------------------------------
# Quick smoke test
# ----------------------------------------------------------------------

if __name__ == "__main__":
    import os
    import sys

    from dotenv import load_dotenv

    load_dotenv()

    logger.remove()
    logger.add(sys.stderr, level="DEBUG")

    # Verify required environment variables
    required_vars = [
        "IG_USER_ID",
        "IG_ACCESS_TOKEN",
        "R2_ACCOUNT_ID",
        "R2_ACCESS_KEY",
        "R2_SECRET_KEY",
        "R2_BUCKET_NAME",
        "R2_PUBLIC_URL",
    ]
    missing = [v for v in required_vars if not os.environ.get(v)]
    if missing:
        logger.error("Missing environment variables: {}", ", ".join(missing))
        sys.exit(1)

    poster = InstagramPoster(
        ig_user_id=os.environ["IG_USER_ID"],
        ig_access_token=os.environ["IG_ACCESS_TOKEN"],
        r2_account_id=os.environ["R2_ACCOUNT_ID"],
        r2_access_key=os.environ["R2_ACCESS_KEY"],
        r2_secret_key=os.environ["R2_SECRET_KEY"],
        r2_bucket_name=os.environ["R2_BUCKET_NAME"],
        r2_public_url=os.environ["R2_PUBLIC_URL"],
    )

    # Test with a sample video (provide path as CLI argument)
    if len(sys.argv) < 2:
        logger.info("Usage: python instagram_poster.py <video_file_path>")
        logger.info("Example: python instagram_poster.py output/2026-03-16/final_reel.mp4")
        sys.exit(0)

    test_video = sys.argv[1]
    if not Path(test_video).exists():
        logger.error("Video file not found: {}", test_video)
        sys.exit(1)

    result = poster.post_reel(
        video_path=test_video,
        caption="Test post from the automated pipeline.\n\n#islam #test",
    )

    logger.info("Result: {}", result)
