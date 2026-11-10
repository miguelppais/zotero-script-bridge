"""Tests for the command-line client, run against a fake bridge on an ephemeral port.

The client's URL is patched to the fake server in setUp, so no test can reach the
real Zotero. Each exit code the client returns (0 to 5, and 130) has at least one test.
"""
import contextlib
import io
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "client"))

import zotero_script_bridge as client  # noqa: E402

# Expected escapes are built from this, never written as double backslashes: two
# backslashes before a name and another backslash read as a network path to the
# public-export scan.
BACKSLASH = chr(92)


def escape(code):
    return f"{BACKSLASH}x{code:02x}"


class FakeBridge:
    """A local HTTP server whose reply is set per test and whose requests are recorded."""

    def __init__(self):
        self.status = 200
        self.body = b""
        self.content_type = "application/json"
        self.close_without_reply = False
        # None, "truncate" (close after a short body), "stall" (hold the connection open)
        # "trickle" (send one body byte at a time for five seconds) or "header_trickle"
        # (send header lines one at a time for five seconds and never finish them).
        self.body_fault = None
        # The handler waits on this event, so a slow reply ends when the test does.
        self.hold = None
        # When set, the reply carries a Location header with this value.
        self.location = None
        self.release = threading.Event()
        self.requests = []
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                bridge.requests.append({
                    "method": self.command,
                    "path": self.path,
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                    "body": self.rfile.read(length),
                })
                if bridge.hold is not None:
                    bridge.release.wait(bridge.hold)
                    # The client has timed out and gone; there is nobody to reply to.
                    return
                if bridge.close_without_reply:
                    self.close_connection = True
                    return
                if bridge.body_fault == "header_trickle":
                    # A status line, then header lines one at a time and never the end of them:
                    # no single wait is long, and the reply never starts.
                    try:
                        self.wfile.write(b"HTTP/1.1 200 OK\r\n")
                        self.wfile.flush()
                        for _ in range(50):
                            self.wfile.write(b"X-Slow: 1\r\n")
                            self.wfile.flush()
                            if bridge.release.wait(0.1):
                                break
                    except OSError:
                        pass
                    self.close_connection = True
                    return
                self.send_response(bridge.status)
                self.send_header("Content-Type", bridge.content_type)
                if bridge.location is not None:
                    self.send_header("Location", bridge.location)
                # A cut-short or stalled body promises more bytes than it delivers.
                promised = len(bridge.body) + (100 if bridge.body_fault else 0)
                if bridge.body_fault == "trickle":
                    promised = 1000
                self.send_header("Content-Length", str(promised))
                self.end_headers()
                if bridge.body_fault == "trickle":
                    # One byte every 0.1 s: each wait is short, the whole reply is not.
                    for _ in range(50):
                        try:
                            self.wfile.write(b"x")
                            self.wfile.flush()
                        except OSError:
                            break
                        if bridge.release.wait(0.1):
                            break
                    self.close_connection = True
                    return
                self.wfile.write(bridge.body)
                self.wfile.flush()
                if bridge.body_fault == "stall":
                    bridge.release.wait(5)
                self.close_connection = True

            # A redirect-following client re-sends as a GET; record that too.
            do_GET = do_POST

            def log_message(self, format, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        # A short poll interval keeps shutdown() from adding half a second per test.
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_address[1]}/script-bridge/execute"

    def reply_json(self, status, payload):
        self.status = status
        self.body = json.dumps(payload).encode("utf-8")
        self.content_type = "application/json"

    def stop(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)


class ClientTestCase(unittest.TestCase):
    def setUp(self):
        self.bridge = FakeBridge()
        self.addCleanup(self.bridge.stop)

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.token_file = self.tmp / "token"
        self.token_file.write_text("  secret-token\n", encoding="utf-8")

        for patcher in (
            mock.patch.object(client, "URL", self.bridge.url),
            mock.patch.object(client, "TOKEN_FILE", self.token_file),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = client.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def run_main_to_bytes(self, *argv):
        """Run with stdout as a cp1252 pipe-like stream; return the exit code and the bytes written."""
        raw = io.BytesIO()
        out = io.TextIOWrapper(raw, encoding="cp1252", newline="\n")
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = client.main(list(argv))
            out.flush()
        return code, raw.getvalue(), err.getvalue()

    def run_main_expecting_exit(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as caught:
                client.main(list(argv))
        return caught.exception.code, out.getvalue(), err.getvalue()


class SuccessTests(ClientTestCase):
    def test_main_returns_0_and_prints_string_result_as_is(self):
        self.bridge.reply_json(200, {"ok": True, "result": "10.0.6"})
        code, out, err = self.run_main("--code", "return 1;")
        self.assertEqual(code, 0)
        self.assertEqual(out, "10.0.6\n")
        self.assertEqual(err, "")

    def test_main_returns_0_and_prints_object_result_as_json(self):
        self.bridge.reply_json(200, {"ok": True, "result": {"count": 3, "name": "café"}})
        code, out, _ = self.run_main("--code", "return {};")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), {"count": 3, "name": "café"})
        self.assertIn("café", out)

    def test_main_returns_0_and_prints_null_for_missing_result(self):
        self.bridge.reply_json(200, {"ok": True})
        code, out, _ = self.run_main("--code", "")
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "null")

    def test_main_ping_sends_version_script(self):
        self.bridge.reply_json(200, {"ok": True, "result": "10.0.6"})
        code, out, _ = self.run_main("--ping")
        self.assertEqual(code, 0)
        self.assertEqual(out, "10.0.6\n")
        self.assertEqual(self.bridge.requests[0]["body"], b"return Zotero.version;")

    def test_main_code_argument_is_sent_as_the_body(self):
        self.bridge.reply_json(200, {"ok": True, "result": "x"})
        self.run_main("--code", "return 'inline';")
        self.assertEqual(self.bridge.requests[0]["body"], b"return 'inline';")

    def test_main_file_argument_sends_file_text_as_utf8(self):
        script = self.tmp / "script.js"
        text = "return 'café – 日本語';"
        script.write_text(text, encoding="utf-8")
        self.bridge.reply_json(200, {"ok": True, "result": "done"})
        code, _, _ = self.run_main(str(script))
        self.assertEqual(code, 0)
        self.assertEqual(self.bridge.requests[0]["body"], text.encode("utf-8"))


class RequestShapeTests(ClientTestCase):
    def test_main_posts_to_endpoint_with_plain_text_and_stripped_token(self):
        self.bridge.reply_json(200, {"ok": True, "result": "x"})
        self.run_main("--code", "return 1;")
        request = self.bridge.requests[0]
        self.assertEqual(request["method"], "POST")
        self.assertEqual(request["path"], "/script-bridge/execute")
        self.assertEqual(request["headers"]["content-type"], "text/plain")
        self.assertEqual(request["headers"]["x-script-bridge-token"], "secret-token")


class ScriptErrorTests(ClientTestCase):
    def test_main_returns_1_and_prints_error_and_stack_on_500(self):
        self.bridge.reply_json(500, {"ok": False, "error": "boom", "stack": "at line 3"})
        code, out, err = self.run_main("--code", "throw new Error('boom');")
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("boom", err)
        self.assertIn("at line 3", err)

    def test_main_returns_1_on_200_with_ok_false(self):
        self.bridge.reply_json(200, {"ok": False, "error": "bad", "stack": "s"})
        code, _, err = self.run_main("--code", "x")
        self.assertEqual(code, 1)
        self.assertIn("bad", err)


class RejectedTests(ClientTestCase):
    def test_main_returns_5_on_413(self):
        self.bridge.reply_json(413, {"ok": False, "error": "body too large"})
        code, out, err = self.run_main("--code", "return 1;")
        self.assertEqual(code, 5)
        self.assertEqual(out, "")
        self.assertIn("rejected (HTTP 413): body too large", err)

    def test_main_returns_5_on_415(self):
        self.bridge.reply_json(415, {"ok": False, "error": "content type"})
        code, _, err = self.run_main("--code", "return 1;")
        self.assertEqual(code, 5)
        self.assertIn("rejected (HTTP 415)", err)

    def test_main_returns_5_on_400_with_json_body(self):
        self.bridge.reply_json(400, {"ok": False, "error": "empty body"})
        code, _, err = self.run_main("--code", " ")
        self.assertEqual(code, 5)
        self.assertIn("rejected (HTTP 400): empty body", err)

    def test_main_still_returns_1_on_500(self):
        self.bridge.reply_json(500, {"ok": False, "error": "boom", "stack": "s"})
        code, _, err = self.run_main("--code", "x")
        self.assertEqual(code, 1)
        self.assertNotIn("rejected", err)


class BadArgumentTests(ClientTestCase):
    def test_main_returns_2_when_token_file_is_missing(self):
        self.token_file.unlink()
        code, out, err = self.run_main("--ping")
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("no token file", err)
        self.assertEqual(self.bridge.requests, [])

    def test_main_exits_2_with_no_arguments(self):
        code, _, err = self.run_main_expecting_exit()
        self.assertEqual(code, 2)
        self.assertIn("usage", err.lower())

    def test_main_exits_2_with_exclusive_arguments(self):
        code, _, err = self.run_main_expecting_exit("--ping", "--code", "return 1;")
        self.assertEqual(code, 2)
        self.assertIn("not allowed with", err)

    def test_main_exits_2_with_file_and_ping(self):
        script = self.tmp / "script.js"
        script.write_text("return 1;", encoding="utf-8")
        code, _, _ = self.run_main_expecting_exit(str(script), "--ping")
        self.assertEqual(code, 2)
        self.assertEqual(self.bridge.requests, [])


class UnreadableInputTests(ClientTestCase):
    def test_main_returns_2_when_script_file_is_missing(self):
        code, out, err = self.run_main(str(self.tmp / "no-such-file.js"))
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("cannot read script file", err)
        self.assertIn("no-such-file.js", err)
        self.assertNotIn("Traceback", err)
        self.assertEqual(self.bridge.requests, [])

    def test_main_returns_2_when_script_file_is_a_directory(self):
        code, _, err = self.run_main(str(self.tmp))
        self.assertEqual(code, 2)
        self.assertIn("cannot read script file", err)
        self.assertEqual(self.bridge.requests, [])

    def test_main_returns_2_when_script_file_is_not_utf8(self):
        script = self.tmp / "latin1.js"
        script.write_bytes("return 'café';".encode("latin-1"))
        code, _, err = self.run_main(str(script))
        self.assertEqual(code, 2)
        self.assertIn("cannot read script file", err)
        self.assertEqual(self.bridge.requests, [])

    def test_main_returns_2_when_token_path_is_a_directory(self):
        with mock.patch.object(client, "TOKEN_FILE", self.tmp):
            code, out, err = self.run_main("--ping")
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("cannot read token file", err)
        self.assertEqual(self.bridge.requests, [])

    def test_main_returns_2_when_token_file_holds_unsendable_characters(self):
        for name, text in (("non-latin-1", "tok日en"), ("newline", "tok\nen"),
                           ("control", "tok\x00en")):
            with self.subTest(name=name):
                self.token_file.write_text(text, encoding="utf-8")
                code, out, err = self.run_main("--ping")
                self.assertEqual(code, 2)
                self.assertEqual(out, "")
                self.assertIn("does not hold a token", err)
                self.assertNotIn("Traceback", err)
                self.assertEqual(self.bridge.requests, [])


class UnreachableTests(ClientTestCase):
    def test_main_returns_3_when_nothing_is_listening(self):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        with mock.patch.object(client, "URL", f"http://127.0.0.1:{port}/script-bridge/execute"):
            code, out, err = self.run_main("--ping")
        self.assertEqual(code, 3)
        self.assertEqual(out, "")
        self.assertIn("not reachable", err)

    def test_main_returns_3_when_server_closes_without_replying(self):
        # What Zotero does with a request it drops before the plugin runs.
        self.bridge.close_without_reply = True
        code, _, err = self.run_main("--ping")
        self.assertEqual(code, 3)
        self.assertIn("connection failed after the script was sent", err)
        self.assertNotIn("not reachable", err)
        self.assertEqual(len(self.bridge.requests), 1)

    def test_main_returns_3_on_http_error_with_non_json_body(self):
        self.bridge.status = 400
        self.bridge.body = b"Bad request"
        self.bridge.content_type = "text/plain"
        code, _, err = self.run_main("--ping")
        self.assertEqual(code, 3)
        self.assertEqual(err, "HTTP 400: Bad request\n")

    def test_main_returns_3_on_http_error_with_empty_body(self):
        self.bridge.status = 404
        self.bridge.body = b""
        code, _, err = self.run_main("--ping")
        self.assertEqual(code, 3)
        self.assertEqual(err, "HTTP 404: (no body)\n")

    def test_main_returns_3_on_http_error_with_json_list_body(self):
        for status in (403, 404, 500):
            with self.subTest(status=status):
                self.bridge.reply_json(status, [1, 2])
                code, out, err = self.run_main("--ping")
                self.assertEqual(code, 3)
                self.assertEqual(out, "")
                self.assertIn(f"HTTP {status}", err)
                self.assertNotIn("Traceback", err)

    def test_main_returns_3_on_http_error_with_json_number_body(self):
        for status in (403, 404, 500):
            with self.subTest(status=status):
                self.bridge.reply_json(status, 42)
                code, _, err = self.run_main("--ping")
                self.assertEqual(code, 3)
                self.assertIn(f"HTTP {status}", err)

    def test_main_returns_3_on_deeply_nested_reply(self):
        # The JSON parser runs out of stack on this instead of raising a ValueError.
        for status in (200, 404):
            with self.subTest(status=status):
                self.bridge.status = status
                self.bridge.body = b"[" * 100000
                code, out, err = self.run_main("--ping")
                self.assertEqual(code, 3)
                self.assertEqual(out, "")
                self.assertNotIn("Traceback", err)

    def test_main_returns_3_on_200_with_non_json_body(self):
        self.bridge.status = 200
        self.bridge.body = b"<html>not the bridge</html>"
        self.bridge.content_type = "text/html"
        code, out, err = self.run_main("--ping")
        self.assertEqual(code, 3)
        self.assertEqual(out, "")
        self.assertIn("unexpected reply", err)
        self.assertIn("not the bridge", err)

    def test_main_returns_3_on_200_with_json_list_body(self):
        self.bridge.reply_json(200, [1, 2, 3])
        code, out, err = self.run_main("--ping")
        self.assertEqual(code, 3)
        self.assertEqual(out, "")
        self.assertIn("unexpected reply", err)

    def test_main_returns_3_with_not_reachable_on_a_connect_error(self):
        error = client.urllib.error.URLError(OSError("network down"))
        with mock.patch.object(client, "open_request", side_effect=error):
            code, out, err = self.run_main("--ping")
        self.assertEqual(code, 3)
        self.assertEqual(out, "")
        self.assertIn("not reachable", err)
        self.assertIn("network down", err)

    def test_main_returns_3_saying_the_script_may_have_run_on_an_error_after_sending(self):
        with mock.patch.object(client, "open_request", side_effect=OSError("reset")):
            code, out, err = self.run_main("--ping")
        self.assertEqual(code, 3)
        self.assertEqual(out, "")
        self.assertIn("the script may have run", err)
        self.assertIn("reset", err)
        self.assertNotIn("not reachable", err)

    def test_main_returns_3_when_error_body_is_cut_short(self):
        self.bridge.reply_json(413, {"ok": False, "error": "body too large"})
        self.bridge.body_fault = "truncate"
        code, out, err = self.run_main("--ping")
        self.assertEqual(code, 3)
        self.assertEqual(out, "")
        self.assertIn("HTTP 413", err)
        self.assertIn("could not read the reply body", err)
        self.assertNotIn("Traceback", err)

    def test_main_returns_3_when_success_body_is_cut_short(self):
        self.bridge.reply_json(200, {"ok": True, "result": "x"})
        self.bridge.body_fault = "truncate"
        code, out, err = self.run_main("--ping")
        self.assertEqual(code, 3)
        self.assertEqual(out, "")
        self.assertIn("the script may have run", err)

    def test_main_returns_3_when_error_body_stalls_past_timeout(self):
        self.bridge.reply_json(413, {"ok": False, "error": "body too large"})
        self.bridge.body_fault = "stall"
        code, out, err = self.run_main("--ping", "--timeout", "1")
        self.assertEqual(code, 3)
        self.assertEqual(out, "")
        self.assertIn("could not read the reply body", err)

    def test_main_returns_3_when_reply_is_slower_than_timeout(self):
        self.bridge.reply_json(200, {"ok": True, "result": "late"})
        self.bridge.hold = 5
        code, out, err = self.run_main("--ping", "--timeout", "0.2")
        self.assertEqual(code, 3)
        self.assertEqual(out, "")
        self.assertIn("the script may have run", err)


class RedirectTests(ClientTestCase):
    def setUp(self):
        super().setUp()
        self.elsewhere = FakeBridge()
        self.addCleanup(self.elsewhere.stop)
        self.elsewhere.reply_json(200, {"ok": True, "result": "wrong place"})

    def test_main_returns_3_and_does_not_follow_a_redirect(self):
        for status in (301, 302, 303, 307, 308):
            with self.subTest(status=status):
                self.bridge.status = status
                self.bridge.location = self.elsewhere.url
                code, out, err = self.run_main("--ping")
                self.assertEqual(code, 3)
                self.assertEqual(out, "")
                self.assertIn(f"unexpected redirect (HTTP {status})", err)
                self.assertIn("not followed", err)
                self.assertNotIn("Traceback", err)
                self.assertEqual(self.elsewhere.requests, [])

    def test_main_returns_3_on_a_redirect_with_a_json_object_body(self):
        self.bridge.reply_json(302, {"ok": False, "error": "moved"})
        self.bridge.location = self.elsewhere.url
        code, out, err = self.run_main("--ping")
        self.assertEqual(code, 3)
        self.assertEqual(out, "")
        self.assertIn("not followed", err)
        self.assertEqual(self.elsewhere.requests, [])

    def test_main_returns_3_on_a_redirect_with_a_malformed_location(self):
        for status in (301, 302, 303, 307, 308):
            with self.subTest(status=status):
                self.bridge.status = status
                self.bridge.location = "http://[::1/"
                code, out, err = self.run_main("--ping")
                self.assertEqual(code, 3)
                self.assertEqual(out, "")
                self.assertIn("not followed", err)
                self.assertNotIn("Traceback", err)

    def test_main_returns_3_on_a_redirect_without_a_location(self):
        self.bridge.status = 304
        code, _, err = self.run_main("--ping")
        self.assertEqual(code, 3)
        self.assertIn("unexpected redirect (HTTP 304)", err)


class ProxyTests(ClientTestCase):
    def test_main_ignores_proxy_settings_in_the_environment(self):
        proxy = FakeBridge()
        self.addCleanup(proxy.stop)
        proxy.reply_json(200, {"ok": True, "result": "via proxy"})
        self.bridge.reply_json(200, {"ok": True, "result": "direct"})
        settings = {name: proxy.url for name in
                    ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy")}
        with mock.patch.dict(os.environ, settings):
            os.environ.pop("NO_PROXY", None)
            os.environ.pop("no_proxy", None)
            code, out, _ = self.run_main("--ping")
        self.assertEqual(code, 0)
        self.assertEqual(out, "direct\n")
        self.assertEqual(len(self.bridge.requests), 1)
        self.assertEqual(proxy.requests, [])


class RefusedTests(ClientTestCase):
    def test_main_returns_4_on_403_with_json_body(self):
        self.bridge.reply_json(403, {"ok": False, "error": "bad token"})
        code, out, err = self.run_main("--ping")
        self.assertEqual(code, 4)
        self.assertEqual(out, "")
        self.assertIn("refused: bad token", err)

    def test_main_returns_4_on_403_without_an_error_key(self):
        self.bridge.reply_json(403, {"ok": False})
        code, _, err = self.run_main("--ping")
        self.assertEqual(code, 4)
        self.assertEqual(err, "refused\n")


class ReplyShapeTests(ClientTestCase):
    def test_main_returns_3_on_a_200_that_is_not_a_success_or_a_failure(self):
        for name, payload in (
            ("empty object", {}),
            ("ok as a string", {"ok": "true", "result": 1}),
            ("ok as a number", {"ok": 1, "result": 1}),
            ("ok missing", {"result": 1}),
            ("ok null", {"ok": None}),
            ("failure without an error", {"ok": False}),
            ("failure with a null error", {"ok": False, "error": None}),
        ):
            with self.subTest(name=name):
                self.bridge.reply_json(200, payload)
                code, out, err = self.run_main("--ping")
                self.assertEqual(code, 3)
                self.assertEqual(out, "")
                self.assertIn("unexpected reply", err)

    def test_main_returns_3_on_a_500_that_is_not_ok_false(self):
        for name, payload in (
            ("ok true", {"ok": True, "result": 1}),
            ("empty object", {}),
            ("ok as a string", {"ok": "false", "error": "x"}),
        ):
            with self.subTest(name=name):
                self.bridge.reply_json(500, payload)
                code, out, err = self.run_main("--ping")
                self.assertEqual(code, 3)
                self.assertEqual(out, "")
                self.assertIn("HTTP 500", err)

    def test_main_returns_3_on_other_error_statuses_with_a_json_object(self):
        for status in (401, 404, 405, 429, 501, 502, 503):
            with self.subTest(status=status):
                self.bridge.reply_json(status, {"ok": False, "error": "from somewhere else"})
                code, out, err = self.run_main("--ping")
                self.assertEqual(code, 3)
                self.assertEqual(out, "")
                self.assertIn(f"HTTP {status}: not the bridge's reply", err)
                self.assertNotIn("rejected", err)

    def test_main_returns_5_only_on_400_413_and_415(self):
        for status in (400, 413, 415):
            with self.subTest(status=status):
                self.bridge.reply_json(status, {"ok": False, "error": "no"})
                code, _, err = self.run_main("--ping")
                self.assertEqual(code, 5)
                self.assertIn(f"rejected (HTTP {status}): no", err)

    def test_main_sends_an_empty_script_file_and_exits_5_when_the_bridge_rejects_it(self):
        script = self.tmp / "empty.js"
        script.write_text("", encoding="utf-8")
        self.bridge.reply_json(400, {"ok": False, "error": "empty body"})
        code, _, err = self.run_main(str(script))
        self.assertEqual(code, 5)
        self.assertEqual(self.bridge.requests[0]["body"], b"")

    def test_main_returns_1_on_a_200_failure_with_an_error(self):
        self.bridge.reply_json(200, {"ok": False, "error": "bad"})
        code, _, err = self.run_main("--ping")
        self.assertEqual(code, 1)
        self.assertIn("script error: bad", err)

    def test_main_returns_3_on_a_500_failure_without_an_error(self):
        for name, payload in (("no error key", {"ok": False}),
                              ("null error", {"ok": False, "error": None})):
            with self.subTest(name=name):
                self.bridge.reply_json(500, payload)
                code, out, err = self.run_main("--ping")
                self.assertEqual(code, 3)
                self.assertEqual(out, "")
                self.assertIn("HTTP 500", err)


class OutputTests(ClientTestCase):
    def test_main_writes_utf8_to_a_stream_with_another_encoding(self):
        self.bridge.reply_json(200, {"ok": True, "result": "中 café"})
        code, written, err = self.run_main_to_bytes("--ping")
        self.assertEqual(code, 0)
        self.assertEqual(written, "中 café\n".encode("utf-8"))
        self.assertEqual(err, "")

    def test_main_returns_3_when_stdout_is_closed(self):
        self.bridge.reply_json(200, {"ok": True, "result": "lost"})
        err = io.StringIO()
        with mock.patch.object(sys, "stdout", None), contextlib.redirect_stderr(err):
            code = client.main(["--ping"])
        self.assertEqual(code, 3)
        self.assertIn("stdout is closed", err.getvalue())
        self.assertEqual(len(self.bridge.requests), 1)

    def test_main_escapes_a_lone_surrogate_instead_of_crashing(self):
        self.bridge.reply_json(200, {"ok": True, "result": "a\ud800b"})
        code, written, err = self.run_main_to_bytes("--ping")
        self.assertEqual(code, 0)
        self.assertEqual(written, b"a" + BACKSLASH.encode() + b"ud800b\n")
        self.assertNotIn("Traceback", err)

    def test_main_escapes_control_characters_in_the_result(self):
        # The output here is not a terminal: a pipe into a terminal emulator is not either.
        hostile = "a\x1b]52;c;AAAA\x07b\x9bc\x7fd\re\nf\tg"
        self.bridge.reply_json(200, {"ok": True, "result": hostile})
        code, out, _ = self.run_main("--ping")
        self.assertEqual(code, 0)
        expected = ("a" + escape(0x1b) + "]52;c;AAAA" + escape(7) + "b" + escape(0x9b) + "c"
                    + escape(0x7f) + "d" + escape(0x0d) + "e\nf\tg\n")
        self.assertEqual(out, expected)

    def test_main_escapes_bidi_and_zero_width_characters(self):
        text = "a\u202eb\u2066c\u2069d\u200be\ufefff\u202ag"
        self.bridge.reply_json(200, {"ok": True, "result": text})
        code, out, _ = self.run_main("--ping")
        self.assertEqual(code, 0)
        u = BACKSLASH + "u"
        self.assertEqual(out, f"a{u}202eb{u}2066c{u}2069d{u}200be{u}fefff{u}202ag\n")

    def test_main_leaves_ordinary_unicode_alone(self):
        text = "café 中文 \U0001F468\u200d\U0001F469 \u05e2\u05d1\u05e8\u05d9\u05ea"
        self.bridge.reply_json(200, {"ok": True, "result": text})
        code, out, _ = self.run_main("--ping")
        self.assertEqual(code, 0)
        self.assertEqual(out, text + "\n")

    def test_main_escapes_control_characters_in_error_and_stack(self):
        self.bridge.reply_json(500, {"ok": False, "error": "e\x1b[2J", "stack": "s\x9b0m"})
        code, _, err = self.run_main("--ping")
        self.assertEqual(code, 1)
        self.assertEqual(err, "script error: e" + escape(0x1b) + "[2J\ns" + escape(0x9b) + "0m\n")

    def test_main_escapes_control_characters_in_a_raw_reply(self):
        self.bridge.status = 200
        self.bridge.body = b"\x1b[31mnot json"
        code, _, err = self.run_main("--ping")
        self.assertEqual(code, 3)
        self.assertNotIn("\x1b", err)
        self.assertIn(escape(0x1b) + "[31mnot json", err)

    def test_main_raw_flag_prints_the_result_unescaped(self):
        self.bridge.reply_json(200, {"ok": True, "result": "a\x1b[0mb\u202ec"})
        code, out, _ = self.run_main("--ping", "--raw")
        self.assertEqual(code, 0)
        self.assertEqual(out, "a\x1b[0mb\u202ec\n")

    def test_main_raw_flag_does_not_unescape_messages_on_stderr(self):
        self.bridge.reply_json(500, {"ok": False, "error": "e\x1b[2J"})
        code, _, err = self.run_main("--ping", "--raw")
        self.assertEqual(code, 1)
        self.assertNotIn("\x1b", err)

    def test_show_prints_nothing_for_a_missing_stream(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            client.show("lost", None)
        self.assertEqual(out.getvalue(), "")

    def test_main_does_not_leak_a_diagnostic_to_stdout_when_stderr_is_closed(self):
        self.bridge.reply_json(404, {"ok": False, "error": "x"})
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.object(sys, "stderr", None):
            code = client.main(["--ping"])
        self.assertEqual(code, 3)
        self.assertEqual(out.getvalue(), "")

    def test_main_ignores_a_stderr_that_cannot_be_written(self):
        self.bridge.reply_json(500, {"ok": False, "error": "boom"})

        class Broken:
            def write(self, text):
                raise BrokenPipeError

            def flush(self):
                raise BrokenPipeError

        with mock.patch.object(sys, "stderr", Broken()):
            code = client.main(["--code", "x"])
        self.assertEqual(code, 1)

    def test_main_prints_a_deeply_nested_result_without_indent_when_indent_overflows(self):
        self.bridge.reply_json(200, {"ok": True, "result": {"a": [1]}})
        real_dumps = json.dumps

        def dumps(obj, **kwargs):
            if kwargs.get("indent"):
                raise RecursionError
            return real_dumps(obj, **kwargs)

        with mock.patch.object(client.json, "dumps", side_effect=dumps):
            code, out, _ = self.run_main("--ping")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), {"a": [1]})

    def test_main_returns_3_when_the_result_cannot_be_printed_at_all(self):
        self.bridge.reply_json(200, {"ok": True, "result": [1]})
        with mock.patch.object(client.json, "dumps", side_effect=RecursionError):
            code, out, err = self.run_main("--ping")
        self.assertEqual(code, 3)
        self.assertEqual(out, "")
        self.assertIn("nested too deeply", err)
        self.assertNotIn("Traceback", err)


class ReplyLimitTests(ClientTestCase):
    def test_main_returns_3_when_the_reply_is_over_the_size_cap(self):
        for status, payload in ((200, {"ok": True, "result": "x" * 5000}),
                                (413, {"ok": False, "error": "x" * 5000})):
            with self.subTest(status=status):
                self.bridge.reply_json(status, payload)
                with mock.patch.object(client, "MAX_REPLY_BYTES", 1000):
                    code, out, err = self.run_main("--ping")
                self.assertEqual(code, 3)
                self.assertEqual(out, "")
                self.assertIn("larger than 1000 bytes", err)

    def test_main_returns_3_when_a_trickled_reply_outlasts_the_timeout(self):
        self.bridge.reply_json(200, {"ok": True, "result": "late"})
        self.bridge.body_fault = "trickle"
        started = time.monotonic()
        code, out, err = self.run_main("--ping", "--timeout", "0.5")
        elapsed = time.monotonic() - started
        self.assertEqual(code, 3)
        self.assertEqual(out, "")
        self.assertIn("not complete within the timeout", err)
        self.assertLess(elapsed, 4)

    def test_main_returns_3_when_a_trickled_error_reply_outlasts_the_timeout(self):
        self.bridge.reply_json(413, {"ok": False, "error": "x"})
        self.bridge.body_fault = "trickle"
        started = time.monotonic()
        code, _, err = self.run_main("--ping", "--timeout", "0.5")
        self.assertEqual(code, 3)
        self.assertIn("HTTP 413", err)
        self.assertIn("not complete within the timeout", err)
        self.assertLess(time.monotonic() - started, 4)

    def test_main_returns_3_when_reading_the_reply_exhausts_memory(self):
        class Response:
            status = 200

            def __init__(self, error):
                self.error = error

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read1(self, n):
                raise self.error

        for error in (MemoryError(), OverflowError("too big")):
            with self.subTest(error=type(error).__name__):
                with mock.patch.object(client, "open_request", return_value=Response(error)):
                    code, out, err = self.run_main("--ping")
                self.assertEqual(code, 3)
                self.assertEqual(out, "")
                self.assertIn("could not be read", err)
                self.assertNotIn("Traceback", err)


class WatchdogTests(ClientTestCase):
    def test_watchdog_ends_the_process_with_3_when_headers_trickle_past_the_timeout(self):
        self.bridge.body_fault = "header_trickle"
        exits = []

        def fake_exit(code):
            exits.append(code)
            # Stand-in for the process ending: let the blocked main thread finish.
            self.bridge.release.set()

        started = time.monotonic()
        with mock.patch.object(client, "WATCHDOG_GRACE", 0.2), \
                mock.patch.object(client.os, "_exit", side_effect=fake_exit):
            code, _, err = self.run_main("--ping", "--timeout", "0.5")
        elapsed = time.monotonic() - started
        self.assertEqual(exits, [3])
        self.assertIn("no complete reply within 0.5 seconds", err)
        self.assertIn("the script may have run", err)
        self.assertEqual(code, 3)
        self.assertLess(elapsed, 3)

    def test_watchdog_is_started_with_the_timeout_plus_the_grace_and_cancelled_after_a_reply(self):
        self.bridge.reply_json(200, {"ok": True, "result": "x"})
        with mock.patch.object(client.threading, "Timer") as timer:
            code, _, _ = self.run_main("--ping", "--timeout", "30")
        self.assertEqual(code, 0)
        self.assertEqual(timer.call_args.args[0], 30 + client.WATCHDOG_GRACE)
        timer.return_value.start.assert_called_once_with()
        timer.return_value.cancel.assert_called_once_with()

    def test_watchdog_is_cancelled_when_the_request_fails(self):
        self.bridge.close_without_reply = True
        with mock.patch.object(client.threading, "Timer") as timer:
            code, _, _ = self.run_main("--ping")
        self.assertEqual(code, 3)
        timer.return_value.cancel.assert_called_once_with()

    def test_watchdog_is_not_started_when_the_inputs_are_refused(self):
        self.token_file.write_text("", encoding="utf-8")
        with mock.patch.object(client.threading, "Timer") as timer:
            code, _, _ = self.run_main("--ping")
        self.assertEqual(code, 2)
        timer.assert_not_called()


class InputFileTests(ClientTestCase):
    def test_main_returns_2_when_the_token_file_is_over_4_kib(self):
        self.token_file.write_text("a" * 5000, encoding="utf-8")
        code, out, err = self.run_main("--ping")
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("cannot read token file", err)
        self.assertIn("larger than 4096 bytes", err)
        self.assertEqual(self.bridge.requests, [])

    def test_main_accepts_a_token_file_of_exactly_4_kib(self):
        self.token_file.write_text("a" * 4096, encoding="utf-8")
        self.bridge.reply_json(200, {"ok": True, "result": "x"})
        code, _, _ = self.run_main("--ping")
        self.assertEqual(code, 0)

    def test_main_reads_a_script_over_the_limit_as_one_mib_and_a_byte_and_exits_5(self):
        script = self.tmp / "big.js"
        script.write_bytes(b"x" * (client.MAX_SCRIPT_BYTES + 5000))
        self.bridge.reply_json(413, {"ok": False, "error": "body too large"})
        code, _, err = self.run_main(str(script))
        self.assertEqual(code, 5)
        self.assertIn("rejected (HTTP 413)", err)
        self.assertEqual(len(self.bridge.requests[0]["body"]), client.MAX_SCRIPT_BYTES + 1)

    def test_main_does_not_fail_on_a_character_cut_by_the_script_limit(self):
        script = self.tmp / "big.js"
        script.write_text("é" * (client.MAX_SCRIPT_BYTES // 2 + 1000), encoding="utf-8")
        self.bridge.reply_json(413, {"ok": False, "error": "body too large"})
        code, _, _ = self.run_main(str(script))
        self.assertEqual(code, 5)
        self.assertGreater(len(self.bridge.requests[0]["body"]), client.MAX_SCRIPT_BYTES)

    def test_main_sends_a_script_of_exactly_the_limit_whole(self):
        script = self.tmp / "edge.js"
        script.write_bytes(b"x" * client.MAX_SCRIPT_BYTES)
        self.bridge.reply_json(200, {"ok": True, "result": "x"})
        code, _, _ = self.run_main(str(script))
        self.assertEqual(code, 0)
        self.assertEqual(len(self.bridge.requests[0]["body"]), client.MAX_SCRIPT_BYTES)

    def test_main_returns_2_for_a_script_or_token_that_is_not_a_regular_file(self):
        with mock.patch.object(client.stat, "S_ISREG", return_value=False):
            code, _, err = self.run_main("--ping")
        self.assertEqual(code, 2)
        self.assertIn("not a regular file", err)
        self.assertEqual(self.bridge.requests, [])
        script = self.tmp / "s.js"
        script.write_text("return 1;", encoding="utf-8")
        calls = iter([True, False])
        with mock.patch.object(client.stat, "S_ISREG", side_effect=lambda mode: next(calls)):
            code, _, err = self.run_main(str(script))
        self.assertEqual(code, 2)
        self.assertIn("cannot read script file", err)
        self.assertEqual(self.bridge.requests, [])


class StartupTests(ClientTestCase):
    def test_main_runs_when_the_module_docstring_is_stripped(self):
        # Python run with -OO has no docstrings.
        self.bridge.reply_json(200, {"ok": True, "result": "ok"})
        with mock.patch.object(client, "__doc__", None):
            code, out, _ = self.run_main("--ping")
        self.assertEqual(code, 0)
        self.assertEqual(out, "ok\n")

    def test_main_returns_2_when_the_home_lookup_raises_key_error(self):
        with mock.patch.object(client, "TOKEN_FILE", None), \
                mock.patch.dict(os.environ):
            os.environ.pop("ZOTERO_SCRIPT_BRIDGE_TOKEN_FILE", None)
            with mock.patch.object(client.Path, "home", side_effect=KeyError("uid")):
                code, _, err = self.run_main("--ping")
        self.assertEqual(code, 2)
        self.assertIn("cannot locate the token file", err)
        self.assertNotIn("Traceback", err)


class TimeoutArgumentTests(ClientTestCase):
    def test_main_exits_2_on_a_timeout_the_socket_layer_would_reject(self):
        for value in ("-1", "0", "nan", "inf", "-inf", "1e300", "86401", "abc", ""):
            with self.subTest(value=value):
                code, _, err = self.run_main_expecting_exit("--ping", f"--timeout={value}")
                self.assertEqual(code, 2)
                self.assertIn("--timeout", err)
                self.assertNotIn("Traceback", err)
                self.assertEqual(self.bridge.requests, [])

    def test_main_accepts_the_largest_timeout(self):
        self.bridge.reply_json(200, {"ok": True, "result": "x"})
        code, _, _ = self.run_main("--ping", "--timeout", "86400")
        self.assertEqual(code, 0)


class ScriptTextTests(ClientTestCase):
    def test_main_returns_2_when_code_cannot_be_encoded(self):
        code, out, err = self.run_main("--code", "return '\ud800';")
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("cannot be encoded as UTF-8", err)
        self.assertNotIn("Traceback", err)
        self.assertEqual(self.bridge.requests, [])


class TokenLocationTests(ClientTestCase):
    def test_main_returns_2_when_the_token_file_is_empty(self):
        for text in ("", "  \n"):
            with self.subTest(text=text):
                self.token_file.write_text(text, encoding="utf-8")
                code, out, err = self.run_main("--ping")
                self.assertEqual(code, 2)
                self.assertEqual(out, "")
                self.assertIn("does not hold a token", err)
                self.assertEqual(self.bridge.requests, [])

    def test_main_reads_the_token_path_from_the_environment_when_none_is_set(self):
        self.bridge.reply_json(200, {"ok": True, "result": "x"})
        with mock.patch.object(client, "TOKEN_FILE", None), \
                mock.patch.dict(os.environ, {"ZOTERO_SCRIPT_BRIDGE_TOKEN_FILE": str(self.token_file)}):
            code, _, _ = self.run_main("--ping")
        self.assertEqual(code, 0)
        self.assertEqual(self.bridge.requests[0]["headers"]["x-script-bridge-token"], "secret-token")

    def test_main_returns_2_when_no_home_folder_can_be_found(self):
        with mock.patch.object(client, "TOKEN_FILE", None), \
                mock.patch.dict(os.environ):
            os.environ.pop("ZOTERO_SCRIPT_BRIDGE_TOKEN_FILE", None)
            with mock.patch.object(client.Path, "home", side_effect=RuntimeError("no home")):
                code, out, err = self.run_main("--ping")
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("cannot locate the token file", err)
        self.assertNotIn("Traceback", err)


class CommandLineEntryTests(unittest.TestCase):
    def run_cli(self, side_effect):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch.object(client, "main", side_effect=side_effect):
            code = client.cli()
        return code, out.getvalue(), err.getvalue()

    def test_cli_returns_130_on_keyboard_interrupt(self):
        code, _, err = self.run_cli(KeyboardInterrupt)
        self.assertEqual(code, 130)
        self.assertEqual(err, "interrupted\n")

    def test_cli_returns_3_on_a_closed_stdout(self):
        code, _, err = self.run_cli(BrokenPipeError)
        self.assertEqual(code, 3)
        self.assertNotIn("Traceback", err)

    def test_cli_returns_what_main_returns(self):
        code, _, _ = self.run_cli(lambda: 4)
        self.assertEqual(code, 4)

    def test_cli_returns_3_with_a_message_on_another_write_error(self):
        code, _, err = self.run_cli(OSError(28, "No space left on device"))
        self.assertEqual(code, 3)
        self.assertIn("cannot write the result", err)
        self.assertIn("No space left on device", err)
        self.assertNotIn("Traceback", err)

    def test_cli_returns_3_on_an_unexpected_failure_and_says_the_script_may_have_run(self):
        for error in (ValueError("boom"), MemoryError(), AttributeError("x")):
            with self.subTest(error=type(error).__name__):
                code, out, err = self.run_cli(error)
                self.assertEqual(code, 3)
                self.assertEqual(out, "")
                self.assertIn("unexpected failure in the client; the script may have run", err)
                self.assertNotIn("Traceback", err)

    def test_cli_lets_argument_errors_through_as_exit_2(self):
        with self.assertRaises(SystemExit) as caught:
            self.run_cli(SystemExit(2))
        self.assertEqual(caught.exception.code, 2)

    def test_cli_keeps_the_exit_code_when_stderr_cannot_be_written(self):
        class Broken:
            def write(self, text):
                raise BrokenPipeError

            def flush(self):
                raise BrokenPipeError

        with mock.patch.object(sys, "stderr", Broken()), \
                mock.patch.object(client, "main", return_value=1), \
                mock.patch.object(client, "discard_stdout") as discard:
            code = client.cli()
        self.assertEqual(code, 1)
        discard.assert_not_called()

    def test_cli_does_not_fail_when_stdout_is_closed(self):
        err = io.StringIO()
        with mock.patch.object(sys, "stdout", None), contextlib.redirect_stderr(err), \
                mock.patch.object(client, "main", return_value=3):
            code = client.cli()
        self.assertEqual(code, 3)
        self.assertEqual(err.getvalue(), "")

    def test_discard_stdout_points_the_descriptor_at_devnull_and_closes_the_extra_one(self):
        fake_stdout = mock.Mock()
        fake_stdout.fileno.return_value = 99
        with mock.patch.object(sys, "stdout", fake_stdout), \
                mock.patch.object(client.os, "open", return_value=7) as opened, \
                mock.patch.object(client.os, "dup2") as dup2, \
                mock.patch.object(client.os, "close") as closed:
            client.discard_stdout()
        opened.assert_called_once()
        dup2.assert_called_once_with(7, 99)
        closed.assert_called_once_with(7)

    def test_discard_stdout_opens_nothing_when_stdout_has_no_descriptor(self):
        with mock.patch.object(sys, "stdout", io.StringIO()), \
                mock.patch.object(client.os, "open") as opened:
            client.discard_stdout()
        opened.assert_not_called()

    def test_discard_stdout_closes_the_extra_descriptor_when_dup2_fails(self):
        fake_stdout = mock.Mock()
        fake_stdout.fileno.return_value = 99
        with mock.patch.object(sys, "stdout", fake_stdout), \
                mock.patch.object(client.os, "open", return_value=7), \
                mock.patch.object(client.os, "dup2", side_effect=OSError), \
                mock.patch.object(client.os, "close") as closed:
            client.discard_stdout()
        closed.assert_called_once_with(7)


if __name__ == "__main__":
    unittest.main()
