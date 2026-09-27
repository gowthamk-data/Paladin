"""Deterministic regression tests for the /ask downstream-failure path.

No Ollama/ChromaDB restart: embedding model, vector store and LLM client are
stubbed to raise the same typed ServiceUnavailableError the real dependencies
raise, immediately after PII redaction commits. In-container tests (see the module note in test_pii_detection); the host
suite skips. Verified run:

  docker cp tests/test_ask_failure_live.py paladin-paladin-api-1:/tmp/
  docker compose exec -T -e PYTHONPATH=/app:/tmp paladin-api \
    python -W ignore -m unittest test_ask_failure_live -v
"""

import os
import sys
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

try:
    import fastapi  # noqa: F401
    import slowapi  # noqa: F401
    import sqlalchemy  # noqa: F401
    _GATEWAY_DEPS = True
except ImportError:
    _GATEWAY_DEPS = False
class AskFailurePathTests(unittest.TestCase):
    def _setup(self):
        import uuid
        from sqlalchemy import CHAR as _CHAR, TypeDecorator as _TD, create_engine
        from sqlalchemy.orm import sessionmaker

        # NOTE: gateway.config binds the key paths at first import and
        # jwt_utils opens the files at import time, so the paths must be
        # correct BEFORE python starts (docker -e flags).
        #
        # Sibling suites may have imported gateway.config earlier with
        # host-relative paths (test_pii_detection rebinds it to
        # <repo>/keys/*.pem), so re-import the config from the real
        # environment here to keep this suite order-independent.
        import importlib
        # A sibling suite may have pointed these at host-relative paths that do
        # not exist in-container; drop such values so gateway.config falls back
        # to its /app/keys default.
        for _var in ("JWT_PRIVATE_KEY_PATH", "JWT_PUBLIC_KEY_PATH"):
            if os.environ.get(_var) and not os.path.exists(os.environ[_var]):
                del os.environ[_var]
        for _mod in ("gateway.config", "gateway.auth.jwt_utils"):
            if _mod in sys.modules:
                del sys.modules[_mod]
        import gateway.config as _cfg
        importlib.reload(_cfg)
        self.assertTrue(os.path.exists(_cfg.JWT_PRIVATE_KEY_PATH),
                        f"missing private key at {_cfg.JWT_PRIVATE_KEY_PATH}; "
                        "export JWT_PRIVATE_KEY_PATH before starting python")
        self.assertTrue(os.path.exists(_cfg.JWT_PUBLIC_KEY_PATH),
                        f"missing public key at {_cfg.JWT_PUBLIC_KEY_PATH}")
        from gateway.db.models import Base, User
        import gateway.routes.query_routes as qr

        class _UuidStr(_TD):
            impl = _CHAR(36)
            cache_ok = True

            def process_bind_param(self, value, dialect):
                return None if value is None else str(value)

            def process_result_value(self, value, dialect):
                return None if value is None else uuid.UUID(str(value))

        # SQLite cannot bind uuid.UUID objects to a native UUID column, so the
        # shared metadata's Postgres UUID types are swapped for CHAR(36) for the
        # duration of this test and restored via addCleanup — other suites in
        # the same process must still see the original types.
        for _table in Base.metadata.tables.values():
            for _col in _table.columns:
                if _col.type.__class__.__name__ == "UUID":
                    original = _col.type
                    _col.type = _UuidStr()
                    self.addCleanup(setattr, _col, "type", original)

        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        session = sessionmaker(bind=engine)()
        self.addCleanup(session.close)
        user = User(username="u1", password_hash="x", department="Engineering",
                    clearance_level=1, role="employee")
        session.add(user)
        session.commit()
        return session, user, qr, uuid

    def _call_ask_with_failure(self, qr, session, user, request_id, raw_message,
                               fail_point):
        import asyncio
        from unittest import mock
        from fastapi import Request
        from gateway.errors import ServiceUnavailableError

        scope = {"type": "http", "method": "POST", "path": "/ask",
                 "query_string": b"", "headers": [], "state": {}}

        async def _receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        request = Request(scope, _receive)
        request.state.correlation_id = str(request_id)
        # NOTE: qr.ask is wrapped by slowapi's @limiter; the decorator's
        # __wrapped__ is slowapi's own sync_wrapper (same Depends-injected
        # signature), NOT the route body — so it cannot be called directly.
        # Deterministic bypass: rebind the module attribute to the undecorated
        # body. The decorator was applied as limiter.limit(...)(ask), and the
        # original function object is retained as __wrapped__ only through
        # slowapi's wrapper chain — instead, reload the module with a no-op
        # limiter so `ask` is the raw coroutine, then restore afterwards.
        import importlib
        import gateway.routes.query_routes as _qrmod
        import gateway.security.rate_limiter as _rlmod
        _real_limit = _rlmod.limiter.limit
        _noop = lambda *a, **k: (lambda fn: fn)
        _rlmod.limiter.limit = _noop
        importlib.reload(_qrmod)
        try:
            ask_fn = _qrmod.ask
            service = {"vector": "vector store",
                       "vector_init": "vector store",
                       "embed_init": "embedding model",
                       "llm": "llm"}[fail_point]
            failing = mock.Mock(
                side_effect=ServiceUnavailableError(service))
            # model.encode(...) must return a numpy-like vector: the route calls
            # .tolist() on the result.
            encode = mock.Mock(
                return_value=mock.Mock(tolist=mock.Mock(return_value=[0.0])))
            if fail_point == "embed_init":
                # The real get_embedding_model() runs so its load-failure handling
                # is exercised; only the underlying sentence_transformers is faked
                # (see the caller) and VectorStore is made to succeed.
                ctx = [mock.patch.object(_qrmod, "VectorStore",
                                         return_value=mock.Mock(
                                             query=mock.Mock(return_value={
                                                 "documents": [["doc one"]],
                                                 "metadatas": [[{"source_document": "s.txt"}]]})))]
            elif fail_point == "vector_init":
                # The VectorStore *constructor* fails, before any query is made.
                # Nothing else is patched: this asserts the init sits inside the
                # guarded downstream block.
                ctx = [mock.patch.object(_qrmod, "VectorStore", failing)]
            elif fail_point == "vector":
                ctx = [mock.patch.object(_qrmod, "VectorStore",
                                         return_value=mock.Mock(query=failing)),
                       mock.patch.object(_qrmod, "get_embedding_model",
                                         return_value=mock.Mock(encode=encode))]
            else:
                ctx = [mock.patch.object(_qrmod, "VectorStore",
                                         return_value=mock.Mock(query=mock.Mock(return_value={
                                             "documents": [["doc one"]],
                                             "metadatas": [[{"source_document": "s.txt"}]]}))),
                       mock.patch.object(_qrmod, "get_embedding_model",
                                         return_value=mock.Mock(encode=encode)),
                       mock.patch.object(_qrmod, "OllamaClient",
                                         return_value=mock.Mock(chat=failing))]
            for patcher in ctx:
                patcher.start()
            try:
                try:
                    # Positional call: the no-op reloaded module keeps the raw
                    # (request, message, db, current_user) coroutine signature.
                    asyncio.run(ask_fn(request, raw_message, session, user))
                except ServiceUnavailableError as exc:
                    qr.LLM_ERROR_STATUS = _qrmod.LLM_ERROR_STATUS
                    qr.LLM_ERROR_RESPONSE = _qrmod.LLM_ERROR_RESPONSE
                    qr._write_llm_error_log = _qrmod._write_llm_error_log
                    return exc
                self.fail("ask() did not raise ServiceUnavailableError")
            finally:
                for patcher in reversed(ctx):
                    patcher.stop()
        finally:
            _rlmod.limiter.limit = _real_limit
            importlib.reload(_qrmod)

    def _assert_single_parent(self, session, qr, request_id, raw_secret):
        from gateway.db.models import PiiEvent, RequestLog

        events = session.query(PiiEvent).filter(
            PiiEvent.request_id == request_id).all()
        self.assertTrue(events, "PII event must be committed before failure")
        rows = session.query(RequestLog).filter(
            RequestLog.request_id == request_id).all()
        self.assertEqual(len(rows), 1, "exactly one parent request_logs row")
        row = rows[0]
        self.assertEqual(row.status, qr.LLM_ERROR_STATUS)
        self.assertEqual(row.llm_response, qr.LLM_ERROR_RESPONSE)
        self.assertEqual((row.prompt_tokens, row.completion_tokens), (0, 0))
        self.assertNotIn(raw_secret, row.sanitized_prompt + row.llm_response)
        self.assertIn("****", row.sanitized_prompt)

    def test_vector_failure_after_pii(self):
        if not _GATEWAY_DEPS:
            self.skipTest("gateway dependency chain not installed (run in-container)")
        session, user, qr, uuid = self._setup()
        try:
            request_id = uuid.uuid4()
            exc = self._call_ask_with_failure(
                qr, session, user, request_id, "my card 4111111111111111 please", "vector")
            self.assertEqual(exc.service, "vector store")
            self._assert_single_parent(session, qr, request_id, "4111111111111111")
            self.assertFalse(qr._write_llm_error_log(session, request_id, user, "x"))
        finally:
            session.close()

    def test_llm_failure_after_pii(self):
        if not _GATEWAY_DEPS:
            self.skipTest("gateway dependency chain not installed (run in-container)")
        session, user, qr, uuid = self._setup()
        try:
            request_id = uuid.uuid4()
            exc = self._call_ask_with_failure(
                qr, session, user, request_id, "my card 4111111111111111 please", "llm")
            self.assertEqual(exc.service, "llm")
            self._assert_single_parent(session, qr, request_id, "4111111111111111")
        finally:
            session.close()


    def test_vector_store_construction_failure_after_pii(self):
        """A failure inside `VectorStore()` must take the typed 503 path.

        Regression test for initialization happening *outside* the guarded
        downstream section: the constructor used to run before the `try`, so its
        ServiceUnavailableError bypassed `_write_llm_error_log` and left the
        committed PII row orphaned (and surfaced as a 500).
        """
        if not _GATEWAY_DEPS:
            self.skipTest("gateway dependency chain not installed (run in-container)")
        from unittest import mock
        from gateway.errors import ServiceUnavailableError

        session, user, qr, uuid = self._setup()
        try:
            request_id = uuid.uuid4()
            exc = self._call_ask_with_failure(
                qr, session, user, request_id, "my card 4111111111111111 please",
                "vector_init")
            # Typed path: a ServiceUnavailableError, not an untyped exception.
            self.assertIsInstance(exc, ServiceUnavailableError)
            self.assertEqual(exc.service, "vector store")
            # Exactly one PII event and exactly one linked llm_error parent row,
            # with a sanitized prompt, safe error text and zero tokens.
            self._assert_single_parent(session, qr, request_id, "4111111111111111")
        finally:
            session.close()

    def test_embedding_model_load_failure_after_pii(self):
        """A model-load failure must be converted to a typed service-unavailable error.

        `get_embedding_model()` previously let import/load exceptions escape
        untyped, which meant a 500 plus an orphaned PII row. It now raises
        ServiceUnavailableError("embedding model") and must therefore write the
        sanitized parent row exactly like the other downstream failures.
        """
        if not _GATEWAY_DEPS:
            self.skipTest("gateway dependency chain not installed (run in-container)")
        from unittest import mock
        from gateway.errors import ServiceUnavailableError

        session, user, qr, uuid = self._setup()
        try:
            request_id = uuid.uuid4()
            # Simulate a real load failure: sentence_transformers is importable
            # but constructing the model raises (missing/corrupt weights, disk
            # full, out of memory). The real get_embedding_model() must convert
            # this into a typed ServiceUnavailableError("embedding model").
            fake_st = mock.Mock(
                SentenceTransformer=mock.Mock(
                    side_effect=OSError("No space left on device")))
            with mock.patch.dict(sys.modules, {"sentence_transformers": fake_st}):
                with mock.patch.object(qr, "_model", None):
                    exc = self._call_ask_with_failure(
                        qr, session, user, request_id, "my card 4111111111111111 please",
                        "embed_init")
            # Typed path, and the original OSError is not exposed to the caller.
            self.assertIsInstance(exc, ServiceUnavailableError)
            self.assertEqual(exc.service, "embedding model")
            self.assertNotIn("No space left", exc.detail)
            self._assert_single_parent(session, qr, request_id, "4111111111111111")
        finally:
            session.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)

