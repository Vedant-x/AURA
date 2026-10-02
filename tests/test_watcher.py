import io
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import aura_watcher


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status = status

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, get_response, post_response):
        self.get_response = get_response
        self.post_response = post_response
        self.upload = None

    def get(self, *_args, **_kwargs):
        return self.get_response

    def post(self, _url, json, **_kwargs):
        self.upload = json
        return self.post_response


class WatcherTests(unittest.TestCase):
    def test_non_successful_poll_is_reported(self):
        session = FakeSession(FakeResponse({}, 404), FakeResponse({}))
        with self.assertRaisesRegex(RuntimeError, "HTTP 404"):
            aura_watcher.check_and_respond(session)

    def test_upload_is_checked_before_reporting_success(self):
        image = SimpleNamespace(
            thumbnail=lambda *_args: None,
            save=lambda buffer, **_kwargs: buffer.write(b"png"),
        )
        session = FakeSession(
            FakeResponse({"question": "what is open?"}),
            FakeResponse({"status": "done"}, 502),
        )
        with patch.object(aura_watcher.pyautogui, "screenshot", return_value=image):
            with self.assertRaisesRegex(RuntimeError, "HTTP 502"):
                aura_watcher.check_and_respond(session)
        self.assertEqual(session.upload["question"], "what is open?")


if __name__ == "__main__":
    unittest.main()
