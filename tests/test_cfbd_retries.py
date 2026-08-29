from __future__ import annotations

from email.message import Message
from io import BytesIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler

from draftscope.data_sources import CFBDClient, _download, _json_request
from draftscope.records import DataError


class CFBDRetryTests(unittest.TestCase):
    @staticmethod
    def _error(code: int) -> HTTPError:
        return HTTPError(
            "https://example.test/player/search",
            code,
            "request failed",
            {},
            BytesIO(b'{"error":"temporary"}'),
        )

    def test_rate_limit_uses_backoff_then_retries(self) -> None:
        client = CFBDClient(
            "real-api-key-123",
            base_url="https://example.test",
            max_retries=2,
            min_request_interval=0,
        )
        with (
            patch(
                "draftscope.data_sources.urlopen",
                side_effect=[self._error(429), BytesIO(b'[{"id":1}]')],
            ) as request,
            patch("draftscope.data_sources.time.sleep") as pause,
        ):
            result = client.get("/player/search", searchTerm="Prospect", year=2026)

        self.assertEqual(result, [{"id": 1}])
        self.assertEqual(request.call_count, 2)
        pause.assert_called_once_with(2.0)

    def test_nonretryable_http_failure_returns_immediately(self) -> None:
        client = CFBDClient(
            "real-api-key-123",
            base_url="https://example.test",
            max_retries=2,
            min_request_interval=0,
        )
        with (
            patch("draftscope.data_sources.urlopen", side_effect=self._error(400)) as request,
            patch("draftscope.data_sources.time.sleep") as pause,
        ):
            with self.assertRaisesRegex(DataError, "after 1 attempt"):
                client.get("/player/search", searchTerm="Prospect", year=2026)

        self.assertEqual(request.call_count, 1)
        pause.assert_not_called()

    def test_rejects_insecure_remote_and_requires_explicit_local_exception(self) -> None:
        with self.assertRaisesRegex(DataError, "must use HTTPS"):
            CFBDClient("real-api-key-123", base_url="http://example.test")
        with self.assertRaisesRegex(DataError, "must use HTTPS"):
            CFBDClient("real-api-key-123", base_url="http://localhost:8080")

        client = CFBDClient(
            "real-api-key-123",
            base_url="http://127.0.0.1:8080",
            allow_insecure_localhost=True,
        )
        self.assertEqual(client.base_url, "http://127.0.0.1:8080")

    def test_bearer_header_is_not_copied_to_cross_origin_redirect(self) -> None:
        requests = []

        def opener(request, *, timeout):
            del timeout
            requests.append(request)
            return BytesIO(b"[]")

        client = CFBDClient(
            "real-api-key-123",
            base_url="https://example.test",
            min_request_interval=0,
            opener=opener,
        )
        self.assertEqual(client.get("/player/search"), [])

        initial = requests[0]
        self.assertEqual(
            initial.unredirected_hdrs["Authorization"],
            "Bearer real-api-key-123",
        )
        self.assertNotIn("Authorization", initial.headers)
        redirected = HTTPRedirectHandler().redirect_request(
            initial,
            None,
            302,
            "Found",
            Message(),
            "https://different-origin.test/player/search",
        )
        self.assertIsNotNone(redirected)
        self.assertIsNone(redirected.get_header("Authorization"))

    def test_cfbd_response_limit_rejects_oversize_payload(self) -> None:
        client = CFBDClient(
            "real-api-key-123",
            base_url="https://example.test",
            min_request_interval=0,
            max_response_bytes=4,
            opener=lambda request, *, timeout: BytesIO(b"[1,2]"),
        )
        with self.assertRaisesRegex(DataError, "exceeded 4 bytes"):
            client.get("/player/search")

    def test_bulk_download_stream_limit_removes_partial_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "asset.csv"
            with (
                patch("draftscope.data_sources._MAX_BULK_DOWNLOAD_BYTES", 4),
                patch(
                    "draftscope.data_sources.urlopen",
                    return_value=BytesIO(b"12345"),
                ),
            ):
                with self.assertRaisesRegex(DataError, "exceeded 4 bytes"):
                    _download(
                        "https://example.test/asset.csv",
                        destination,
                        refresh=True,
                    )

            self.assertFalse(destination.exists())
            self.assertEqual(list(Path(directory).glob(".asset.csv.*")), [])

    def test_wikimedia_retries_rate_limits_and_server_errors(self) -> None:
        rate_limit = HTTPError(
            "https://en.wikipedia.org/w/api.php",
            429,
            "rate limited",
            {"Retry-After": "3"},
            BytesIO(b"slow down"),
        )
        server_error = HTTPError(
            "https://en.wikipedia.org/w/api.php",
            503,
            "unavailable",
            {},
            BytesIO(b"retry"),
        )
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch(
                    "draftscope.data_sources.urlopen",
                    side_effect=[
                        rate_limit,
                        server_error,
                        BytesIO(b'{"query":{}}'),
                    ],
                ) as request,
                patch("draftscope.data_sources.time.sleep") as pause,
                patch(
                    "draftscope.data_sources._WIKIMEDIA_MIN_REQUEST_INTERVAL_SECONDS",
                    0,
                ),
            ):
                payload = _json_request(
                    "https://en.wikipedia.org/w/api.php?action=query",
                    Path(directory) / "query.json",
                    refresh=True,
                )

        self.assertEqual(payload, {"query": {}})
        self.assertEqual(request.call_count, 3)
        self.assertEqual([call.args[0] for call in pause.call_args_list], [3.0, 2.0])


if __name__ == "__main__":
    unittest.main()
