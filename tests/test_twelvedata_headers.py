"""Header parsing uses fake HTTP responses, never provider requests."""
import os
import unittest
from unittest.mock import Mock, patch

import requests
from screener import data


class HeaderTests(unittest.TestCase):
    def test_transport_failure_cannot_reuse_previous_cost(self):
        data.TWELVEDATA_LAST_REQUEST_CREDITS = 1
        data.TWELVEDATA_LAST_CREDITS_LEFT = 7
        with patch.dict(os.environ, TWELVEDATA_API_KEY="offline"), \
             patch.object(data.SESSION, "get", side_effect=requests.Timeout("offline")):
            with self.assertRaises(data.TwelveDataError):
                data._twelvedata("ABC")
        self.assertEqual(data.get_twelvedata_credit_state(), (None, None))

    def test_invalid_cost_headers_are_unknown_not_clamped_to_one(self):
        for value in (None, "0", "-1", "1.2", "NaN", "n/a"):
            response = Mock(status_code=200, headers={"api-credits-request": value})
            response.json.return_value = {"values": []}
            with self.subTest(value=value), patch.dict(os.environ, TWELVEDATA_API_KEY="offline"), \
                 patch.object(data.SESSION, "get", return_value=response):
                with self.assertRaises(data.TwelveDataUnavailableError):
                    data._twelvedata("ABC")
                self.assertIsNone(data.get_twelvedata_credit_state()[0])

    def test_error_responses_also_expose_current_request_cost(self):
        for code in (401, 403, 429, 500):
            response = Mock(status_code=code, headers={"api-credits-request": "8"}, text="offline")
            with self.subTest(code=code), patch.dict(os.environ, TWELVEDATA_API_KEY="offline"), \
                 patch.object(data.SESSION, "get", return_value=response):
                with self.assertRaises(data.TwelveDataError):
                    data._twelvedata("ABC")
                self.assertEqual(data.get_twelvedata_credit_state()[0], 8)

    def test_malformed_json_does_not_lose_cost_information(self):
        response = Mock(status_code=200, headers={"api-credits-request": "1"})
        response.json.side_effect = ValueError("invalid JSON")
        with patch.dict(os.environ, TWELVEDATA_API_KEY="offline"), \
             patch.object(data.SESSION, "get", return_value=response):
            with self.assertRaises(data.TwelveDataError):
                data._twelvedata("ABC")
        self.assertEqual(data.get_twelvedata_credit_state()[0], 1)
