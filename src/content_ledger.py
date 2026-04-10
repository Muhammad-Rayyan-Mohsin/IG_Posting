"""
Content Ledger Module
---------------------
Manages a Google Sheets content ledger that tracks all generated content,
prevents repetition of Islamic references, and serves as a monitoring dashboard.
"""

import json
import os
import tempfile
import time
from datetime import datetime, timedelta

import gspread
from loguru import logger
from oauth2client.service_account import ServiceAccountCredentials
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

SCOPES = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive",
]

HEADERS = [
    "Date",
    "Category",
    "Title",
    "Script Preview",
    "Sources",
    "Hashtag Set ID",
    "Video URL",
    "Instagram Post ID",
    "Status",
    "Error",
    "Created At",
    "Duration (min)",
]


class ContentLedger:
    """Reads and writes a Google Sheets spreadsheet that acts as the single
    source of truth for every piece of content the pipeline produces."""

    def __init__(self, credentials_json: str, spreadsheet_id: str):
        """
        Parameters
        ----------
        credentials_json : str
            Either a file path ending in ``.json`` pointing to a Google service
            account key file, **or** the raw JSON string itself (useful when
            the key is stored in an environment variable on Railway).
        spreadsheet_id : str
            The Google Sheets spreadsheet ID (the long string in the URL).
        """
        self.spreadsheet_id = spreadsheet_id
        self._records_cache = None
        self._cache_time = 0
        self._cache_ttl = 30  # seconds
        self._authenticate(credentials_json)
        self._open_spreadsheet()

    # ------------------------------------------------------------------
    # Authentication & setup
    # ------------------------------------------------------------------

    def _authenticate(self, credentials_json: str) -> None:
        """Build gspread credentials from a file path or raw JSON string."""
        try:
            if credentials_json.rstrip().endswith(".json"):
                logger.info("Authenticating with service account key file: {}", credentials_json)
                creds = ServiceAccountCredentials.from_json_keyfile_name(
                    credentials_json, SCOPES
                )
            else:
                logger.info("Authenticating with inline service account JSON")
                key_data = json.loads(credentials_json)
                # gspread needs a file path, so write to a temp file.
                # Use try/finally to guarantee the file is removed even if
                # from_json_keyfile_name raises (prevents private key leakage).
                tmp = tempfile.NamedTemporaryFile(
                    mode="w", suffix=".json", delete=False
                )
                try:
                    json.dump(key_data, tmp)
                    tmp.flush()
                    tmp.close()
                    creds = ServiceAccountCredentials.from_json_keyfile_name(
                        tmp.name, SCOPES
                    )
                finally:
                    if os.path.exists(tmp.name):
                        os.unlink(tmp.name)

            self.gc = gspread.authorize(creds)
            logger.info("Google Sheets authentication successful")
        except Exception as exc:
            logger.error("Failed to authenticate with Google Sheets: {}", exc)
            raise

    def _open_spreadsheet(self) -> None:
        """Open the spreadsheet and get (or create) the main worksheet."""
        try:
            self.spreadsheet = self.gc.open_by_key(self.spreadsheet_id)
            logger.info("Opened spreadsheet: {}", self.spreadsheet.title)
        except gspread.exceptions.SpreadsheetNotFound:
            logger.error(
                "Spreadsheet with ID '{}' not found. "
                "Make sure the spreadsheet exists and is shared with the service account.",
                self.spreadsheet_id,
            )
            raise
        except Exception as exc:
            logger.error("Failed to open spreadsheet: {}", exc)
            raise

        # Use the first worksheet (or create one named "Content Ledger")
        try:
            self.worksheet = self.spreadsheet.sheet1
            logger.info("Using worksheet: {}", self.worksheet.title)
        except Exception as exc:
            logger.error("Failed to access worksheet: {}", exc)
            raise

        self._ensure_headers()

    # ------------------------------------------------------------------
    # Header management
    # ------------------------------------------------------------------

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type(gspread.exceptions.APIError),
        reraise=True,
    )
    def _ensure_headers(self) -> None:
        """Create the header row if the sheet is empty or headers are missing."""
        try:
            first_row = self.worksheet.row_values(1)
        except Exception:
            first_row = []

        if first_row != HEADERS:
            if first_row:
                logger.warning(
                    "Header mismatch detected — overwriting row 1 with correct headers"
                )
            else:
                logger.info("Sheet is empty — writing header row")
            self.worksheet.update("A1", [HEADERS])
            logger.info("Headers written: {}", HEADERS)

    # ------------------------------------------------------------------
    # Reading helpers
    # ------------------------------------------------------------------

    def _get_all_records(self) -> list[dict]:
        """Return every data row as a list of dicts (header-keyed), with a short TTL cache."""
        now = time.time()
        if self._records_cache is not None and (now - self._cache_time) < self._cache_ttl:
            return self._records_cache
        records = self._fetch_all_records()
        self._records_cache = records
        self._cache_time = now
        return records

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type(gspread.exceptions.APIError),
        reraise=True,
    )
    def _fetch_all_records(self) -> list[dict]:
        """Fetch every data row from the sheet (network call)."""
        return self.worksheet.get_all_records()

    def _invalidate_cache(self):
        """Clear the cached records so the next read hits the sheet."""
        self._records_cache = None
        self._cache_time = 0

    def get_used_references(self) -> list[str]:
        """Return a flat list of every Islamic reference that has already been
        used across all previously generated content.

        References are extracted from the *Sources* column, which stores a JSON
        array of dicts — each dict is expected to have a ``"ref"`` key (e.g.
        ``"Quran 2:255"``, ``"Sahih Bukhari 6018"``).
        """
        records = self._get_all_records()
        references: list[str] = []

        for row in records:
            sources_raw = row.get("Sources", "")
            if not sources_raw:
                continue
            try:
                sources = json.loads(sources_raw)
                for src in sources:
                    if isinstance(src, dict):
                        # Support both "reference" (system prompt format) and "ref"
                        ref = src.get("reference") or src.get("ref")
                        if ref:
                            references.append(ref)
                    elif isinstance(src, str):
                        references.append(src)
            except (json.JSONDecodeError, TypeError):
                logger.warning("Could not parse Sources JSON: {}", sources_raw)

        logger.info("Found {} previously used references", len(references))
        return references

    def get_last_hashtag_set_id(self) -> int:
        """Return the ``hashtag_set_id`` from the most recent ledger row.

        Returns 0 if the sheet has no data rows.
        """
        records = self._get_all_records()
        if not records:
            logger.info("No existing rows — returning hashtag_set_id=0")
            return 0

        last_row = records[-1]
        try:
            set_id = int(last_row.get("Hashtag Set ID", 0))
        except (ValueError, TypeError):
            set_id = 0

        logger.info("Last hashtag_set_id: {}", set_id)
        return set_id

    def get_recent_entries(self, days: int = 30) -> list[dict]:
        """Return ledger entries from the last *days* days.

        Each entry is a dict keyed by the header names.
        """
        records = self._get_all_records()
        cutoff = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
        recent = [r for r in records if r.get("Date", "") >= cutoff]
        logger.info(
            "Returning {} entries from the last {} days (cutoff {})",
            len(recent),
            days,
            cutoff,
        )
        return recent

    # ------------------------------------------------------------------
    # Writing helpers
    # ------------------------------------------------------------------

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type(gspread.exceptions.APIError),
        reraise=True,
    )
    def log_entry(
        self,
        date: str,
        category: str,
        title: str,
        script: str,
        sources: list[dict],
        hashtag_set_id: int,
    ) -> int:
        """Append a new row to the ledger with ``status='generated'``.

        Parameters
        ----------
        date : str
            Content date in ``YYYY-MM-DD`` format.
        category : str
            Content category (e.g. ``"Quran Reflection"``).
        title : str
            Title / hook of the video.
        script : str
            Full script text — only the first 100 characters are stored.
        sources : list[dict]
            List of source dicts, each with at least a ``"ref"`` key.
        hashtag_set_id : int
            Which hashtag rotation set was used.

        Returns
        -------
        int
            The 1-based row number of the newly appended row.
        """
        script_preview = script[:100] if script else ""
        sources_json = json.dumps(sources, ensure_ascii=False)
        created_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

        new_row = [
            date,
            category,
            title,
            script_preview,
            sources_json,
            hashtag_set_id,
            "",  # Video URL — filled later
            "",  # Instagram Post ID — filled later
            "generated",
            "",  # Error — empty for now
            created_at,
            "",  # Duration (min) — filled later
        ]

        self.worksheet.append_row(new_row, value_input_option="USER_ENTERED")

        # Locate the appended row by its unique Created At timestamp (column 11).
        # This avoids a race condition where len(get_all_values()) returns a stale
        # count if another process appended a row between our append and the count.
        cell = self.worksheet.find(created_at, in_column=11)
        row_number = cell.row if cell else len(self.worksheet.get_all_values())
        logger.info(
            "Logged new entry at row {} — title='{}', status='generated'",
            row_number,
            title,
        )
        self._invalidate_cache()
        return row_number

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type(gspread.exceptions.APIError),
        reraise=True,
    )
    def update_status(self, row: int, status: str, **kwargs) -> None:
        """Update the status (and optional fields) for a given row.

        Parameters
        ----------
        row : int
            1-based row number in the sheet.
        status : str
            New status value — one of ``generated``, ``assembled``,
            ``posted``, or ``failed``.
        **kwargs
            Optional keyword arguments that map to specific columns:

            - ``video_url`` -> Video URL column
            - ``instagram_post_id`` -> Instagram Post ID column
            - ``error_message`` -> Error column
        """
        # Column indices (1-based) matching HEADERS
        col_map = {
            "Video URL": 7,
            "Instagram Post ID": 8,
            "Status": 9,
            "Error": 10,
        }

        kwarg_to_col = {
            "video_url": "Video URL",
            "instagram_post_id": "Instagram Post ID",
            "error_message": "Error",
        }

        # Build a single batch update: collect all (col_index, value) pairs
        # starting with Status, then any provided kwargs.
        updates: list[tuple[int, str]] = [(col_map["Status"], status)]
        for kwarg_key, col_name in kwarg_to_col.items():
            if kwarg_key in kwargs and kwargs[kwarg_key] is not None:
                updates.append((col_map[col_name], str(kwargs[kwarg_key])))

        # Sort by column index so the range arithmetic is straightforward.
        updates.sort(key=lambda x: x[0])

        if updates:
            min_col = updates[0][0]
            max_col = updates[-1][0]
            # Build a single row of values spanning min_col..max_col,
            # leaving unchanged columns as empty strings.
            row_values = [""] * (max_col - min_col + 1)
            for col_idx, value in updates:
                row_values[col_idx - min_col] = value

            # Convert column index to letter (A=1, G=7, H=8, I=9, J=10)
            def col_letter(n: int) -> str:
                result = ""
                while n > 0:
                    n, remainder = divmod(n - 1, 26)
                    result = chr(65 + remainder) + result
                return result

            range_notation = (
                f"{col_letter(min_col)}{row}:{col_letter(max_col)}{row}"
            )
            self.worksheet.update(range_notation, [row_values])
            logger.info(
                "Row {} batch-updated {} — status='{}'",
                row,
                range_notation,
                status,
            )

        self._invalidate_cache()


# ----------------------------------------------------------------------
# Quick smoke test
# ----------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    logger.remove()
    logger.add(sys.stderr, level="DEBUG")

    creds_path = os.environ.get("GOOGLE_CREDENTIALS_JSON", "credentials.json")
    sheet_id = os.environ.get("GOOGLE_SPREADSHEET_ID", "")

    if not sheet_id:
        logger.error(
            "Set GOOGLE_SPREADSHEET_ID env var before running this test"
        )
        sys.exit(1)

    ledger = ContentLedger(creds_path, sheet_id)

    # Show used references
    refs = ledger.get_used_references()
    logger.info("Used references ({}): {}", len(refs), refs)

    # Show last hashtag set
    last_set = ledger.get_last_hashtag_set_id()
    logger.info("Last hashtag set ID: {}", last_set)

    # Log a test entry
    test_row = ledger.log_entry(
        date=datetime.utcnow().strftime("%Y-%m-%d"),
        category="Test",
        title="Smoke Test Entry",
        script="This is a test script to verify the content ledger is working correctly.",
        sources=[{"ref": "Test Reference 1"}, {"ref": "Test Reference 2"}],
        hashtag_set_id=last_set + 1,
    )
    logger.info("Test entry logged at row {}", test_row)

    # Update status
    ledger.update_status(test_row, "posted", video_url="https://example.com/test.mp4")
    logger.info("Test entry status updated to 'posted'")

    # Recent entries
    recent = ledger.get_recent_entries(days=7)
    logger.info("Recent entries (last 7 days): {}", len(recent))
    for entry in recent:
        logger.info("  {} | {} | {}", entry.get("Date"), entry.get("Title"), entry.get("Status"))

    logger.info("Smoke test complete")
