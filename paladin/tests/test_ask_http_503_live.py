"""HTTP-level (ASGI) regression test for the /ask 503 path.

Complements the direct-handler tests in test_ask_failure_live.py: this one
drives the whole FastAPI application as an ASGI callable, so it exercises
routing, DI overrides, the slowapi limiter, the correlation middleware, the
exception handler in gateway/errors.py and the real HTTP status code (503)
rather than a raised exception.

The request is issued through a minimal in-process ASGI driver rather than
``fastapi.testclient.TestClient``, because TestClient pulls in ``httpx`` which
is not part of the runtime image; the driver below needs nothing beyond the
already-installed dependencies.

The downstream failure is injected by patching the VectorStore / OllamaClient
symbols in gateway.routes.query_routes, so no service is stopped and no
container is restarted. The DB session and the authenticated user are supplied
via FastAPI dependency overrides backed by an in-memory SQLite database, so no
row is written to the real PostgreSQL instance and no historical audit row is
touched.

Only two requests are made, far below the burst allowance, so the real
rate limiter is left in place on purpose (the 503 path must work *through* it).
"""
import os
import sys
import unittest
import uuid as _uuid
from unittest import mock

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

try:
    import anyio
    import fastapi
    import slowapi
    import sqlalchemy

    _GATEWAY_DEPS = True
except ImportError:
    _GATEWAY_DEPS = False


class AsgiResponse:
    """Minimal captured ASGI response (status, headers, body)."""

    def __init__(self, status, headers, body):
        self.status_code = status
        self.raw_headers = headers
        self.text = body.decode("utf-8", "replace")

    @property
    def headers(self):
        return {k.decode().lower(): v.decode() for k, v in self.raw_headers}

    def json(self):
        import json

        return json.loads(self.text)


def call_asgi(app, path, query_string, method="POST"):
    """Invoke an ASGI app in-process and capture the response.

    Replaces fastapi.testclient.TestClient (which needs httpx, absent from the
    runtime image). The lifespan protocol is run first because the gateway's
    middleware stack is built at startup.
    """
    import asyncio
    import json

    body = json.dumps({}).encode()

    async def _main():
        # The body is delivered once, then the client "disconnects". Without the
        # disconnect the logging middleware's listen_for_disconnect task never
        # completes and the ASGI call hangs.
        state = {"sent": False}

        async def _receive():
            if not state["sent"]:
                state["sent"] = True
                return {"type": "http.request", "body": body, "more_body": False}
            await asyncio.sleep(3600)
            return {"type": "http.disconnect"}

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": query_string.encode(),
            "root_path": "",
            "headers": [
                (b"host", b"testserver"),
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
        }
        status = 500
        headers = []
        chunks = []

        async def _send(message):
            nonlocal status, headers
            if message["type"] == "http.response.start":
                status = message["status"]
                headers = message.get("headers", [])
            elif message["type"] == "http.response.body":
                chunks.append(message.get("body", b""))

        await app(scope, _receive, _send)
        return AsgiResponse(status, headers, b"".join(chunks))

    return asyncio.run(_main())


@unittest.skipUnless(_GATEWAY_DEPS, "gateway dependency chain not installed (run in-container)")
class AskHttp503Tests(unittest.TestCase):
    """Drive /ask through the ASGI app with a simulated downstream outage."""

    def setUp(self):
        # gateway.config binds the key paths at import time; a sibling suite may
        # have pointed them at host-relative paths that do not exist in-container.
        for var in ("JWT_PRIVATE_KEY_PATH", "JWT_PUBLIC_KEY_PATH"):
            if os.environ.get(var) and not os.path.exists(os.environ[var]):
                del os.environ[var]
        import importlib

        for mod in ("gateway.config", "gateway.auth.jwt_utils"):
            sys.modules.pop(mod, None)
        import gateway.config as cfg

        importlib.reload(cfg)

        from gateway.main import app
        from gateway.db.models import Base, PiiEvent, RequestLog, User
        from gateway.routes import query_routes as qr

        self.qr = qr
        self.PiiEvent = PiiEvent
        self.RequestLog = RequestLog

        # SQLite cannot bind uuid.UUID to a native UUID column, so swap the
        # shared metadata's Postgres UUID types for CHAR(36) for this test only
        # and restore them afterwards so sibling suites are unaffected.
        from sqlalchemy import CHAR, TypeDecorator, create_engine
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import StaticPool

        class UuidStr(TypeDecorator):
            impl = CHAR(36)
            cache_ok = True

            def process_bind_param(self, value, dialect):
                return None if value is None else str(value)

            def process_result_value(self, value, dialect):
                return None if value is None else _uuid.UUID(str(value))

        for table in Base.metadata.tables.values():
            for col in table.columns:
                if col.type.__class__.__name__ == "UUID":
                    original = col.type
                    col.type = UuidStr()
                    self.addCleanup(setattr, col, "type", original)

        # StaticPool + check_same_thread=False is required: `ask` is a sync route
        # so Starlette runs it in an anyio worker thread, and a default SQLite
        # :memory: engine hands each thread its own empty database.
        engine = create_engine(
            "sqlite:///:memory:",
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
        Base.metadata.create_all(engine)
        self.session = sessionmaker(bind=engine)()
        self.addCleanup(self.session.close)
        self.user = User(
            username="u1", password_hash="x", department="Engineering",
            clearance_level=1, role="employee",
        )
        self.session.add(self.user)
        self.session.commit()

        # Auth and DB are the only two things stubbed; routing, the limiter, the
        # correlation middleware and the exception handlers all run for real.
        app.dependency_overrides[qr.get_db] = lambda: self.session
        app.dependency_overrides[qr.get_current_user] = lambda: self.user
        self.addCleanup(app.dependency_overrides.clear)

        self.app = app

    def _run_ask_with_outage(self, fail_point, raw_message):
        """POST /ask with a simulated downstream failure; return the response."""
        from gateway.errors import ServiceUnavailableError

        service = "vector store" if fail_point == "vector" else "llm"
        failing = mock.Mock(side_effect=ServiceUnavailableError(service))
        # model.encode(...) must return a numpy-like vector: the route calls
        # .tolist() on the result.
        encode = mock.Mock(
            return_value=mock.Mock(tolist=mock.Mock(return_value=[0.0])))
        if fail_point == "vector":
            patches = [
                mock.patch.object(self.qr, "VectorStore",
                                  return_value=mock.Mock(query=failing)),
                mock.patch.object(self.qr, "get_embedding_model",
                                  return_value=mock.Mock(encode=encode)),
            ]
        else:
            patches = [
                mock.patch.object(
                    self.qr, "VectorStore",
                    return_value=mock.Mock(query=mock.Mock(return_value={
                        "documents": [["doc one"]],
                        "metadatas": [[{"source_document": "s.txt"}]],
                    })),
                ),
                mock.patch.object(self.qr, "get_embedding_model",
                                  return_value=mock.Mock(encode=encode)),
                mock.patch.object(self.qr, "OllamaClient",
                                  return_value=mock.Mock(chat=failing)),
            ]
        for patcher in patches:
            patcher.start()
        try:
            import urllib.parse

            qs = urllib.parse.urlencode({"message": raw_message})
            return call_asgi(self.app, "/ask", qs)
        finally:
            for patcher in reversed(patches):
                patcher.stop()

    def _assert_sanitized_audit_row(self, request_id, raw_secret):
        events = self.session.query(self.PiiEvent).filter(
            self.PiiEvent.request_id == request_id).all()
        self.assertTrue(events, "PII event must be committed before the failure")
        rows = self.session.query(self.RequestLog).filter(
            self.RequestLog.request_id == request_id).all()
        self.assertEqual(len(rows), 1, "exactly one parent request_logs row")
        row = rows[0]
        self.assertEqual(row.status, self.qr.LLM_ERROR_STATUS)
        self.assertEqual(row.llm_response, self.qr.LLM_ERROR_RESPONSE)
        self.assertEqual((row.prompt_tokens, row.completion_tokens), (0, 0))
        self.assertNotIn(raw_secret, row.sanitized_prompt + row.llm_response)
        self.assertIn("****", row.sanitized_prompt)

    def test_vector_store_outage_returns_503_with_audit_row(self):
        secret = "4111111111111111"
        resp = self._run_ask_with_outage("vector", f"my card {secret} please")
        self.assertEqual(resp.status_code, 503, resp.text)
        body = resp.json()
        self.assertEqual(body["dependency"], "vector store")
        # The 503 body must not leak internals or the raw PII.
        self.assertNotIn("Traceback", resp.text)
        self.assertNotIn(secret, resp.text)
        # The correlation id must be echoed in both the body and the header.
        self.assertEqual(body["request_id"], resp.headers["x-request-id"])
        self._assert_sanitized_audit_row(_uuid.UUID(body["request_id"]), secret)

    def test_llm_outage_returns_503_with_audit_row(self):
        secret = "4111111111111111"
        resp = self._run_ask_with_outage("llm", f"my card {secret} please")
        self.assertEqual(resp.status_code, 503, resp.text)
        body = resp.json()
        self.assertEqual(body["dependency"], "llm")
        self.assertNotIn(secret, resp.text)
        self._assert_sanitized_audit_row(_uuid.UUID(body["request_id"]), secret)


if __name__ == "__main__":
    unittest.main(verbosity=2)

