"""Deterministic OpenAI-compatible chat-completions stub for acceptance tests.

Runs only inside the compose `test` profile. It answers every chat completion
request with one fixed, schema-valid fact candidate so the end-to-end
extraction scenario completes without calling a paid external model.
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, HTTPServer

_CANDIDATE = {
    "subject": "样例物质",
    "property": "熔点",
    "value": "125",
    "unit": "℃",
    "evidence_text": "样例物质 熔点 125 ℃",
    "graph_fact_key": "mock-llm:sample-melting-point",
    "source_locator": "table-1:r1:c1",
}

_RESPONSE = {
    "id": "mock-llm-completion",
    "object": "chat.completion",
    "created": 0,
    "model": "mock-e2e",
    "choices": [
        {
            "index": 0,
            "message": {
                "role": "assistant",
                "content": json.dumps([_CANDIDATE], ensure_ascii=False),
            },
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
}


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - stdlib handler name
        if self.path == "/health":
            self._reply(200, {"status": "ok"})
        else:
            self._reply(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler name
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(min(length, 1_000_000))
        self._reply(200, _RESPONSE)

    def _reply(self, status: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:
        pass


if __name__ == "__main__":
    HTTPServer(("0.0.0.0", 8000), _Handler).serve_forever()
