#!/usr/bin/env python3
"""Unit tests for bin/oauth-token-helper using a mock token endpoint on loopback.

Run:  python3 -m unittest discover -s tests -v
"""

import base64
import importlib.machinery
import importlib.util
import io
import json
import logging
import os
import socket
import stat
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
import socketserver
from http.server import BaseHTTPRequestHandler, HTTPServer

os.environ.setdefault("no_proxy", "127.0.0.1,localhost")
os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")

HELPER_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bin", "oauth-token-helper")


def load_helper():
    loader = importlib.machinery.SourceFileLoader("oauth_token_helper", HELPER_PATH)
    spec = importlib.util.spec_from_loader("oauth_token_helper", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


helper = load_helper()

SECRET = "s3cr3t-value-that-must-never-be-logged"
CLIENT_ID = "11111111-2222-3333-4444-555555555555"
TENANT_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def make_jwt(claims: dict) -> str:
    header = b64url(json.dumps({"typ": "JWT", "alg": "none"}).encode())
    payload = b64url(json.dumps(claims).encode())
    return "%s.%s.%s" % (header, payload, b64url(b"signature-bytes"))


def outlook_claims(**overrides) -> dict:
    now = int(time.time())
    claims = {
        "aud": "https://outlook.office365.com",
        "iss": "https://sts.windows.net/%s/" % TENANT_ID,
        "appid": CLIENT_ID,
        "tid": TENANT_ID,
        "roles": ["SMTP.SendAsApp"],
        "ver": "1.0",
        "iat": now, "nbf": now, "exp": now + 3599,
        "oid": "should-not-be-printed",
    }
    claims.update(overrides)
    return claims


class LoopbackHTTPServer(HTTPServer):
    """HTTPServer without the getfqdn() reverse lookup in server_bind (30s on some hosts)."""

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        self.server_name = "127.0.0.1"
        self.server_port = self.server_address[1]


class MockTokenEndpoint:
    """Scripted HTTP token endpoint.  Each request pops the next (status, body, headers)."""

    def __init__(self):
        self.responses = []
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                form = urllib.parse.parse_qs(self.rfile.read(length).decode())
                outer.requests.append({k: v[0] for k, v in form.items()})
                status, body, headers = outer.responses.pop(0) if outer.responses else (500, {"error": "unscripted"}, {})
                payload = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                for k, v in headers.items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):  # silence
                pass

        self.server = LoopbackHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return "http://127.0.0.1:%d/token" % self.server.server_address[1]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class CaptureLogs:
    def __init__(self):
        self.stream = io.StringIO()

    def __enter__(self):
        helper.setup_logging(self.stream)
        return self

    def __exit__(self, *exc):
        helper.setup_logging(sys.stderr)

    @property
    def text(self) -> str:
        return self.stream.getvalue()


class HelperTestCase(unittest.TestCase):
    def setUp(self):
        self.endpoint = MockTokenEndpoint()
        self.tmpdir = tempfile.TemporaryDirectory()
        self.token_file = os.path.join(self.tmpdir.name, "sender.tokens.json")
        self.env = {
            "OAUTH_TENANT_ID": TENANT_ID,
            "OAUTH_CLIENT_ID": CLIENT_ID,
            "OAUTH_CLIENT_SECRET": SECRET,
            "OAUTH_TOKEN_ENDPOINT": self.endpoint.url,
            "OAUTH_TOKEN_FILE": self.token_file,
            "OAUTH_TOKEN_FILE_OWNER": "",
        }
        self.cfg = helper.load_config(self.env)

    def tearDown(self):
        self.endpoint.close()
        self.tmpdir.cleanup()

    def success_response(self, expires_in=3599, claims=None):
        return (200, {"token_type": "Bearer", "expires_in": expires_in,
                      "access_token": make_jwt(claims or outlook_claims())}, {})

    # -- config -------------------------------------------------------------

    def test_effective_mode_defaults(self):
        self.assertEqual(helper.effective_mode({}), "refresh_token")
        self.assertEqual(helper.effective_mode({"OAUTH_TENANT_ID": "x"}), "client_credentials")
        self.assertEqual(helper.effective_mode({"OAUTH_GRANT_TYPE": "refresh_token", "OAUTH_TENANT_ID": "x"}), "refresh_token")
        self.assertEqual(helper.effective_mode({"OAUTH_GRANT_TYPE": "Client_Credentials"}), "client_credentials")
        with self.assertRaises(helper.ConfigError):
            helper.effective_mode({"OAUTH_GRANT_TYPE": "password"})

    def test_load_config_reports_every_missing_item(self):
        with self.assertRaises(helper.ConfigError) as ctx:
            helper.load_config({"OAUTH_GRANT_TYPE": "client_credentials"})
        message = str(ctx.exception)
        for name in ("OAUTH_TENANT_ID", "OAUTH_CLIENT_ID", "OAUTH_CLIENT_SECRET"):
            self.assertIn(name, message)

    def test_secret_file_wins_over_env_and_is_stripped(self):
        path = os.path.join(self.tmpdir.name, "secret")
        with open(path, "w") as f:
            f.write("  from-file \n")
        env = dict(self.env, OAUTH_CLIENT_SECRET_FILE=path)
        cfg = helper.load_config(env)
        self.assertEqual(cfg.client_secret, "from-file")
        self.assertIn(path, cfg.secret_source)
        self.assertNotIn("from-file", repr(cfg))

    def test_default_endpoint_and_scope(self):
        env = dict(self.env)
        del env["OAUTH_TOKEN_ENDPOINT"]
        cfg = helper.load_config(env)
        self.assertEqual(cfg.token_endpoint, "https://login.microsoftonline.com/%s/oauth2/v2.0/token" % TENANT_ID)
        self.assertEqual(cfg.scope, "https://outlook.office365.com/.default")
        self.assertEqual(cfg.refresh_margin, 300)

    def test_plain_http_only_on_loopback(self):
        with self.assertRaises(helper.ConfigError):
            helper.load_config(dict(self.env, OAUTH_TOKEN_ENDPOINT="http://login.example.com/token"))
        helper.load_config(dict(self.env, OAUTH_TOKEN_ENDPOINT="http://localhost:1/token"))  # no raise

    # -- acquisition and token file ----------------------------------------

    def test_success_writes_sasl_xoauth2_compatible_file(self):
        self.endpoint.responses.append(self.success_response())
        token = helper.acquire_token(self.cfg)
        helper.write_token_file(self.token_file, token, self.cfg)

        request = self.endpoint.requests[0]
        self.assertEqual(request["grant_type"], "client_credentials")
        self.assertEqual(request["client_id"], CLIENT_ID)
        self.assertEqual(request["client_secret"], SECRET)
        self.assertEqual(request["scope"], "https://outlook.office365.com/.default")

        with open(self.token_file) as f:
            doc = json.load(f)
        self.assertEqual(doc["access_token"], token.access_token)
        self.assertEqual(doc["refresh_token"], "")           # key required by sasl-xoauth2
        self.assertIsInstance(doc["expiry"], str)            # parsed with stoi(asString())
        self.assertEqual(int(doc["expiry"]), token.expiry)
        self.assertGreaterEqual(token.expiry, int(time.time()) + 3599 - 5)
        self.assertEqual(doc["token_endpoint"], self.endpoint.url)
        mode = stat.S_IMODE(os.stat(self.token_file).st_mode)
        self.assertEqual(mode, 0o600)
        leftovers = [n for n in os.listdir(self.tmpdir.name) if n != "sender.tokens.json"]
        self.assertEqual(leftovers, [], "temporary file left behind")
        self.assertNotIn(token.access_token, repr(token))

    def test_expires_in_as_string_is_accepted(self):
        self.endpoint.responses.append((200, {"access_token": make_jwt(outlook_claims()), "expires_in": "3600"}, {}))
        self.assertEqual(helper.acquire_token(self.cfg).expires_in, 3600)

    def test_http_error_reports_status_and_aad_error_without_secret(self):
        self.endpoint.responses.append((401, {
            "error": "invalid_client",
            "error_description": "AADSTS7000215: Invalid client secret provided. Trace ID: t-1",
            "error_codes": [7000215], "trace_id": "t-1", "correlation_id": "c-1",
        }, {}))
        with self.assertRaises(helper.TokenError) as ctx:
            helper.acquire_token(self.cfg)
        text = str(ctx.exception)
        self.assertIn("HTTP 401", text)
        self.assertIn("invalid_client", text)
        self.assertIn("AADSTS7000215", text)
        self.assertIn("correlation_id=c-1", text)
        self.assertNotIn(SECRET, text)
        self.assertEqual(ctx.exception.status, 401)

    def test_non_json_error_body_is_truncated(self):
        self.endpoint.responses.append((502, b"<html>" + b"x" * 5000 + b"</html>", {}))
        with self.assertRaises(helper.TokenError) as ctx:
            helper.acquire_token(self.cfg)
        self.assertIn("HTTP 502", str(ctx.exception))
        self.assertLess(len(str(ctx.exception)), 800)

    def test_connection_refused_is_a_token_error(self):
        # Grab a free loopback port and release it: connecting to it is refused
        # immediately on every OS (port 1 is silently dropped on macOS).
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        cfg = helper.load_config(dict(self.env, OAUTH_TOKEN_ENDPOINT="http://127.0.0.1:%d/token" % port,
                                      OAUTH_HTTP_TIMEOUT="3"))
        with self.assertRaises(helper.TokenError) as ctx:
            helper.acquire_token(cfg)
        self.assertIn("token request failed", str(ctx.exception))

    def test_existing_token_validity(self):
        self.assertIsNone(helper.existing_token_validity(self.token_file))
        now = int(time.time())
        with open(self.token_file, "w") as f:
            json.dump({"access_token": "x", "refresh_token": "", "expiry": str(now + 100)}, f)
        self.assertAlmostEqual(helper.existing_token_validity(self.token_file, now), 100, delta=1)
        with open(self.token_file, "w") as f:
            json.dump({"access_token": "x", "refresh_token": "", "expiry": str(now - 1)}, f)
        self.assertIsNone(helper.existing_token_validity(self.token_file, now))

    # -- daemon loop ---------------------------------------------------------

    def run_daemon_cycles(self, cycles: int):
        stop = threading.Event()
        waits = []

        def fake_wait(seconds):
            waits.append(seconds)
            if len(waits) >= cycles:
                stop.set()

        with CaptureLogs() as logs:
            helper.run_daemon(self.cfg, stop, wait_fn=fake_wait)
        return waits, logs.text

    def test_daemon_renews_before_expiry_with_margin(self):
        self.endpoint.responses.append(self.success_response(expires_in=3599))
        waits, text = self.run_daemon_cycles(1)
        self.assertEqual(waits, [3599 - 300])
        self.assertIn("OAuth access token refreshed; expires in 3599 seconds", text)
        self.assertTrue(os.path.exists(self.token_file))

    def test_daemon_backs_off_and_keeps_existing_token_on_failure(self):
        now = int(time.time())
        with open(self.token_file, "w") as f:
            json.dump({"access_token": "OLD-TOKEN", "refresh_token": "", "expiry": str(now + 1800)}, f)
        self.endpoint.responses.append((500, {"error": "server_error", "error_description": "boom"}, {}))
        self.endpoint.responses.append((503, {"error": "temporarily_unavailable"}, {}))
        self.endpoint.responses.append(self.success_response())
        waits, text = self.run_daemon_cycles(3)

        self.assertGreaterEqual(waits[0], int(helper.BACKOFF_INITIAL * 0.8))
        self.assertLessEqual(waits[0], int(helper.BACKOFF_INITIAL * 1.2) + 1)
        self.assertGreaterEqual(waits[1], int(helper.BACKOFF_INITIAL * 2 * 0.8))
        self.assertEqual(waits[2], 3599 - 300)
        self.assertIn("HTTP 500 server_error: boom", text)
        self.assertIn("keeping existing token, still valid for", text)
        self.assertIn("OAuth access token refreshed", text)
        with open(self.token_file) as f:
            self.assertNotEqual(json.load(f)["access_token"], "OLD-TOKEN")

    def test_daemon_honours_retry_after(self):
        self.endpoint.responses.append((429, {"error": "throttled"}, {"Retry-After": "7"}))
        waits, text = self.run_daemon_cycles(1)
        self.assertEqual(waits, [7])
        self.assertIn("no valid token on disk", text)

    def test_daemon_never_logs_secret_or_token(self):
        self.endpoint.responses.append((400, {"error": "invalid_request", "error_description": "bad"}, {}))
        self.endpoint.responses.append(self.success_response())
        _, text = self.run_daemon_cycles(2)
        self.assertNotIn(SECRET, text)
        with open(self.token_file) as f:
            self.assertNotIn(json.load(f)["access_token"], text)
        self.assertNotIn("eyJ", text)

    def test_short_lived_token_does_not_spin(self):
        self.endpoint.responses.append(self.success_response(expires_in=120))
        waits, text = self.run_daemon_cycles(1)
        self.assertEqual(waits, [60])
        self.assertIn("shorter than OAUTH_REFRESH_MARGIN", text)

    # -- JWT inspection ------------------------------------------------------

    def test_jwt_claims_and_role_check(self):
        claims = helper.decode_jwt_claims(make_jwt(outlook_claims()))
        self.assertEqual(claims["tid"], TENANT_ID)
        names = [name for name, _ in helper.safe_claims(claims)]
        self.assertIn("aud", names)
        self.assertIn("roles", names)
        self.assertNotIn("oid", names)
        self.assertEqual(helper.smtp_role_check(claims)[0], True)
        self.assertEqual(helper.smtp_role_check(outlook_claims(roles=["Mail.Read"]))[0], False)
        self.assertEqual(helper.smtp_role_check(outlook_claims(roles=None))[0], False)
        self.assertEqual(helper.smtp_role_check(outlook_claims(aud="https://graph.microsoft.com", roles=[]))[0], True)
        self.assertIsNone(helper.decode_jwt_claims("opaque-token"))
        self.assertEqual(helper.smtp_role_check(None)[0], True)

    # -- sasl-xoauth2.conf -----------------------------------------------------

    def test_sasl_config_uses_placeholder_secret(self):
        doc = helper.sasl_config_document(self.cfg)
        self.assertEqual(doc["client_id"], CLIENT_ID)
        self.assertNotEqual(doc["client_secret"], SECRET)
        self.assertEqual(doc["token_endpoint"], self.endpoint.url)


if __name__ == "__main__":
    unittest.main()
