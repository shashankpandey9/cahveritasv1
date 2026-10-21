"""LLM calls through Databricks Model Serving, against a mock workspace.

Checks the request the app sends (endpoint per tier, chat messages, image
parts, auth header) and how it reads replies (string or list content,
retries, content filter).
"""
import asyncio
import json
import unittest
from unittest.mock import patch

import httpx

from backend.app import config, dbx, llm

HOST = "https://adb-123.azuredatabricks.net"


class ServingTest(unittest.TestCase):
    def setUp(self):
        self._saved = (config.LLM_PROVIDER, config.MODEL_FAST, config.MODEL_SMART,
                       config.MODEL_VISION, config.VISION_ENABLED, llm._client)
        config.LLM_PROVIDER = "databricks"
        config.MODEL_FAST, config.MODEL_SMART, config.MODEL_VISION = (
            "claude-fast-endpoint", "claude-smart-endpoint", "claude-vision-endpoint")
        config.VISION_ENABLED = True
        self.requests = []
        self.replies = []
        self.patches = [patch.object(dbx, "host", return_value=HOST),
                        patch.object(dbx, "auth_headers", return_value={"Authorization": "Bearer t"})]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        (config.LLM_PROVIDER, config.MODEL_FAST, config.MODEL_SMART,
         config.MODEL_VISION, config.VISION_ENABLED, llm._client) = self._saved

    def _serve(self):
        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            status, body = self.replies.pop(0)
            return httpx.Response(status, json=body)
        llm._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    @staticmethod
    def _chat(content, finish="stop"):
        return {"choices": [{"message": {"role": "assistant", "content": content},
                             "finish_reason": finish}]}

    def test_smart_call_goes_to_its_endpoint_with_system_and_user(self):
        self._serve()
        self.replies = [(200, self._chat("design ok"))]
        out = asyncio.run(llm.smart("trace R7", system="You are an agent", cached_context="CORPUS"))
        self.assertEqual(out, "design ok")
        req = self.requests[0]
        self.assertEqual(str(req.url), f"{HOST}/serving-endpoints/claude-smart-endpoint/invocations")
        self.assertEqual(req.headers["authorization"], "Bearer t")
        body = json.loads(req.content)
        self.assertEqual([m["role"] for m in body["messages"]], ["system", "user"])
        self.assertIn("CORPUS", body["messages"][0]["content"])
        self.assertEqual(body["messages"][1]["content"], "trace R7")
        self.assertEqual(body["max_tokens"], 1600)
        self.assertNotIn("cache_control", req.content.decode())

    def test_fast_tier_and_list_content(self):
        self._serve()
        self.replies = [(200, self._chat([{"type": "reasoning", "summary": []},
                                          {"type": "text", "text": '{"Relevant": "Yes"}'}]))]
        out = asyncio.run(llm.fast("determine"))
        self.assertEqual(out, '{"Relevant": "Yes"}')
        self.assertIn("/claude-fast-endpoint/", str(self.requests[0].url))

    def test_vision_sends_image_part_to_vision_endpoint(self):
        self._serve()
        self.replies = [(200, self._chat('{"title": "Flow"}'))]
        out = asyncio.run(llm.vision("transcribe", b"\x89PNGdata", "image/png",
                                     model=config.MODEL_VISION, system="sys"))
        self.assertEqual(out, '{"title": "Flow"}')
        self.assertIn("/claude-vision-endpoint/", str(self.requests[0].url))
        user = json.loads(self.requests[0].content)["messages"][1]["content"]
        self.assertEqual(user[0]["type"], "image_url")
        self.assertTrue(user[0]["image_url"]["url"].startswith("data:image/png;base64,"))
        self.assertEqual(user[1], {"type": "text", "text": "transcribe"})

    def test_retry_on_429_then_success(self):
        self._serve()
        self.replies = [(429, {"error": "rate"}), (200, self._chat("ok"))]
        with patch.object(asyncio, "sleep", return_value=None):
            out = asyncio.run(llm.smart("x"))
        self.assertEqual(out, "ok")
        self.assertEqual(len(self.requests), 2)

    def test_content_filter_and_errors_return_none(self):
        self._serve()
        self.replies = [(200, self._chat("", finish="content_filter")),
                        (404, {"error_code": "RESOURCE_DOES_NOT_EXIST"})]
        self.assertIsNone(asyncio.run(llm.smart("x")))
        self.assertIsNone(asyncio.run(llm.smart("y")))

    def test_unconfigured_endpoints_mean_heuristic_mode(self):
        config.MODEL_FAST = config.MODEL_SMART = ""
        self.assertFalse(llm.available())
        self.assertIsNone(asyncio.run(llm.smart("x")))


if __name__ == "__main__":
    unittest.main()
