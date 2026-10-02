import importlib
import os
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import time
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "test-key")
main = importlib.import_module("main")


class FakeTable:
    def __init__(self):
        self.inserted = []
        self.fail_insert = False

    def select(self, *_args):
        return self

    def order(self, *_args, **_kwargs):
        return self

    def limit(self, *_args):
        return self

    def insert(self, data):
        if self.fail_insert:
            raise RuntimeError("storage down")
        self.inserted.append(data)
        return self

    def execute(self):
        return SimpleNamespace(data=[])


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.memories = FakeTable()
        self.logs = FakeTable()
        main.supabase = SimpleNamespace(
            table=lambda name: {"memories": self.memories, "screen_logs": self.logs}[name]
        )
        with main.screen_lock:
            main.screen_state.update(
                pending_question=None, active_question=None, started_at=None, result=None
            )
        self.http = TestClient(main.app)

    def test_failed_ai_call_is_not_saved_as_memory(self):
        with patch.object(main.client.messages, "create", side_effect=RuntimeError("upstream")):
            response = self.http.post("/chat", json={"text": "hello"})
        self.assertEqual(response.status_code, 502)
        self.assertEqual(self.memories.inserted, [])

    def test_optional_token_blocks_unauthorized_calls_when_enabled(self):
        with patch.dict(os.environ, {"AURA_API_TOKEN": "test-secret"}):
            self.assertEqual(self.http.get("/screen-pending").status_code, 401)
            allowed = self.http.get(
                "/screen-pending", headers={"Authorization": "Bearer test-secret"}
            )
            self.assertEqual(allowed.status_code, 200)

    def test_one_watcher_claims_the_question(self):
        response = self.http.post("/screen-request", json={"question": "what is open?"})
        self.assertEqual(response.status_code, 200)
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: main.screen_pending()["question"], range(8)))
        self.assertEqual(results.count("what is open?"), 1)
        self.assertEqual(results.count(None), 7)

    def test_wrong_upload_cannot_answer_active_request(self):
        self.http.post("/screen-request", json={"question": "my screen"})
        self.http.get("/screen-pending")
        response = self.http.post(
            "/screen-upload", json={"question": "stale question", "image_base64": "AA=="}
        )
        self.assertEqual(response.status_code, 409)
        self.assertIsNone(main.screen_state["result"])

    def test_screen_answer_survives_log_failure(self):
        self.logs.fail_insert = True
        self.http.post("/screen-request", json={"question": "my screen"})
        self.http.get("/screen-pending")
        reply = SimpleNamespace(content=[SimpleNamespace(type="text", text="A window")])
        with patch.object(main.client.messages, "create", return_value=reply):
            response = self.http.post(
                "/screen-upload", json={"question": "my screen", "image_base64": "AA=="}
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.http.get("/screen-result").json()["reply"], "A window")

    def test_phone_screen_question_reaches_the_correct_answer(self):
        def answer(**kwargs):
            model = kwargs["model"]
            text = "YES" if model == main.CLASSIFY_MODEL else "A settings window"
            return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)])

        with patch.object(main.client.messages, "create", side_effect=answer):
            with ThreadPoolExecutor(max_workers=1) as pool:
                phone = pool.submit(
                    self.http.post, "/ask", json={"text": "What is on my screen?"}
                )
                pending = None
                for _ in range(30):
                    pending = self.http.get("/screen-pending").json()["question"]
                    if pending:
                        break
                    time.sleep(0.05)
                self.assertEqual(pending, "What is on my screen?")
                upload = self.http.post(
                    "/screen-upload",
                    json={"question": pending, "image_base64": "AA=="},
                )
                self.assertEqual(upload.status_code, 200)
                self.assertEqual(phone.result(timeout=3).json()["reply"], "A settings window")


if __name__ == "__main__":
    unittest.main()
