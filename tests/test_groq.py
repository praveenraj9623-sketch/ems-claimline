import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from claimline.groq import GroqClient, GroqError


class Fake(BaseHTTPRequestHandler):
    log = []
    mode = "ok"

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).log.append(body)
        kind = body["response_format"]["type"]
        if type(self).mode == "no-schema" and kind == "json_schema":
            status, payload = 400, {"error": "unsupported"}
        elif type(self).mode == "unauthorized":
            status, payload = 401, {}
        else:
            status, payload = 200, {"choices": [{"message": {"content": json.dumps({"a": True})}}]}
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


class GroqTests(unittest.TestCase):
    def setUp(self):
        Fake.log, Fake.mode = [], "ok"
        self.http = HTTPServer(("127.0.0.1", 0), Fake)
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.client = GroqClient("key", base_url=f"http://127.0.0.1:{self.http.server_address[1]}", sleep=lambda s: None)

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()

    def test_strict_schema_first(self):
        self.assertEqual(self.client.json_object("s", "u", {"type": "object"}), {"a": True})
        self.assertEqual(Fake.log[0]["response_format"]["type"], "json_schema")

    def test_falls_back_to_json_mode(self):
        Fake.mode = "no-schema"
        self.assertEqual(self.client.json_object("s", "u", {"type": "object"}), {"a": True})
        self.assertEqual([b["response_format"]["type"] for b in Fake.log], ["json_schema", "json_object"])

    def test_bad_key_is_explained(self):
        Fake.mode = "unauthorized"
        with self.assertRaises(GroqError) as ctx:
            self.client.json_object("s", "u", {"type": "object"})
        self.assertIn("GROQ_API_KEY", str(ctx.exception))

    def test_disabled_without_key(self):
        self.assertFalse(GroqClient("").enabled)


if __name__ == "__main__":
    unittest.main()
