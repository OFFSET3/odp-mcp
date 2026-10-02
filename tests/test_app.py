import os
import unittest
from unittest.mock import AsyncMock, patch

import app


class HeaderTests(unittest.TestCase):
    def test_distinct_auth_headers(self):
        with patch.dict(
            os.environ,
            {
                "USPTO_API_KEY": "odp-test-key",
                "PATENTSVIEW_API_KEY": "pv-test-key",
                "USPTO_TSDR_API_KEY": "tsdr-test-key",
            },
            clear=False,
        ):
            self.assertEqual(app._odp_headers()["X-API-KEY"], "odp-test-key")
            self.assertEqual(app._patentsview_headers()["X-Api-Key"], "pv-test-key")
            self.assertEqual(app._tsdr_headers()["USPTO-API-KEY"], "tsdr-test-key")

    def test_tsdr_falls_back_without_exposing_key(self):
        import httpx

        with patch.dict(os.environ, {"USPTO_API_KEY": "secret-value"}, clear=True):
            headers = app._tsdr_headers()
            self.assertEqual(headers["USPTO-API-KEY"], "secret-value")

            request = httpx.Request("GET", "https://tsdrapi.uspto.gov/example")
            response = httpx.Response(401, request=request)
            exc = httpx.HTTPStatusError("unauthorized", request=request, response=response)
            error = app._upstream_error(exc, service="TSDR")
            self.assertNotIn("secret-value", repr(error))
            self.assertEqual(error["status_code"], 401)


class AsyncToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_odp_fallback_normalizes_application(self):
        payload = {
            "count": 1,
            "patentFileWrapperDataBag": [
                {
                    "applicationMetaData": {
                        "patentNumber": "12345678",
                        "inventionTitle": "Example system",
                        "grantDate": "2026-01-01",
                        "applicationNumberText": "18123456",
                        "applicationStatusDescriptionText": "Patented Case",
                        "filingDate": "2024-01-01",
                    }
                }
            ],
        }
        with patch.dict(os.environ, {"USPTO_API_KEY": "odp-test-key"}, clear=True):
            with patch.object(app, "_get", new=AsyncMock(return_value=payload)) as mocked:
                result = await app._odp_patent_search_fallback("example", page=1, per_page=25)
        self.assertEqual(result["patents"][0]["patent_number"], "12345678")
        self.assertEqual(result["source"], "USPTO ODP Patent File Wrapper")
        self.assertIn("/search", mocked.await_args.args[0])

    async def test_odp_fallback_accepts_flattened_patent_bag(self):
        payload = {
            "count": 1,
            "patentBag": [
                {
                    "patentNumber": "9999999",
                    "inventionTitle": "Flattened example",
                    "grantDate": "2025-01-01",
                    "applicationNumberText": "17123456",
                    "applicationStatusDescriptionText": "Patented Case",
                }
            ],
        }
        with patch.dict(os.environ, {"USPTO_API_KEY": "odp-test-key"}, clear=True):
            with patch.object(app, "_get", new=AsyncMock(return_value=payload)):
                result = await app._odp_patent_search_fallback("flattened", page=1, per_page=25)
        self.assertEqual(result["patents"][0]["patent_number"], "9999999")
        self.assertEqual(result["patents"][0]["application_number"], "17123456")

    async def test_application_status_uses_odp_file_wrapper(self):
        with patch.dict(os.environ, {"USPTO_API_KEY": "odp-test-key"}, clear=True):
            with patch.object(app, "_get", new=AsyncMock(return_value={"ok": True})) as mocked:
                result = await app.odp_application_status("18/123,456")
        self.assertTrue(result["ok"])
        self.assertTrue(mocked.await_args.args[0].endswith("/18123456"))
        self.assertIn("X-API-KEY", mocked.await_args.kwargs["headers"])

    async def test_trademark_status_uses_tsdr_header_and_case_prefix(self):
        env = {"USPTO_API_KEY": "odp-test-key", "USPTO_TSDR_API_KEY": "tsdr-test-key"}
        with patch.dict(os.environ, env, clear=True):
            with patch.object(app, "_get", new=AsyncMock(return_value={"ok": True})) as mocked:
                result = await app.odp_trademark_status("97-123456")
        self.assertTrue(result["ok"])
        self.assertTrue(mocked.await_args.args[0].endswith("/casestatus/sn97123456/info.json"))
        self.assertIn("USPTO-API-KEY", mocked.await_args.kwargs["headers"])

    async def test_retired_tools_fail_explicitly_without_network(self):
        fulltext = await app.odp_patent_fulltext_search("machine learning")
        trademark = await app.odp_trademark_search("AresNet")
        self.assertEqual(fulltext["status_code"], 410)
        self.assertFalse(fulltext["supported"])
        self.assertEqual(trademark["status_code"], 501)
        self.assertFalse(trademark["supported"])


if __name__ == "__main__":
    unittest.main()
