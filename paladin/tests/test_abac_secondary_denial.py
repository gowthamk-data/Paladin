"""Isolated unit test for the ABAC secondary-denial defense-in-depth path.

The mocked vector-store result intentionally BYPASSES the normal Chroma ABAC
filter: the fake collection returns a department-scoped Finance document
(classification_level=2, department="Finance", visibility="department") for an
Engineering user whose clearance (2) would otherwise satisfy the level check.
Under the deployed predicate (``build_chroma_filter`` in
gateway/authz/policy.py) that document can never reach the result set of a
normal request — the filter excludes it before retrieval. The fake therefore
simulates *unexpected or inconsistent retrieval metadata* (e.g. policy/filter
drift, a metadata mutation, or a bug in a future ingestion path).

What this test verifies: the defense-in-depth secondary check inside
``VectorStore.query`` re-authorizes every returned chunk via the real
``authorize()`` policy and raises ``AuthorizationDeniedError`` with reason
``DEPARTMENT_MISMATCH``, which the /ask route converts to HTTP 403 and exactly
one DenialLog row (request id, user id, user department/clearance, reason
code, sanitized prompt) while never forwarding the retrieved document text to
the response or the LLM.

What it does NOT establish: it is not evidence that a normal request produces
an ABAC denial — a normal request never sees this document because the
Chroma filter excludes it before the secondary check runs — and it proves the
denial-row *construction* against a fake (in-memory SQLite) session, not live
PostgreSQL persistence.

Conventions mirror tests/test_ask_http_503_live.py: in-process ASGI driver,
FastAPI dependency overrides for auth and DB, unittest-style (no pytest
required), and no connection to PostgreSQL, ChromaDB, Ollama or any running
stack — every downstream dependency is faked.
"""
import os
import sys
import types
import unittest
import uuid as _uuid
from unittest import mock

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

# Import the in-process ASGI driver and response shim from the sibling suite
# rather than duplicating ~60 lines; both suites are collected/run together.
from tests.test_ask_http_503_live import AsgiResponse, call_asgi  # noqa: E402,F401

# The poisoned chunk: its text carries a marker that must never appear in the
# HTTP response or in any audit-row column.
SECRET_DOC_TEXT = "CONFIDENTIAL-FINANCE-SECRET-BUDGET-FIGURES"
FINANCE_METADATA = {
    "source_document": "finance_guidelines.txt",
    "classification_level": 2,
    "department": "Finance",
    "visibility": "department",
}


@unittest.skipUnless(_GATEWAY_DEPS, "gateway dependency chain not installed (run in-container)")
class AbacSecondaryDenialTests(unittest.TestCase):
    """Drive /ask with a fake Chroma result that violates the ABAC filter."""

    def setUp(self):
        # gateway.config binds key paths at import time; mirror the sibling
        # suite's reset so a host-relative path cannot break the import.
        for var in ("JWT_PRIVATE_KEY_PATH", "JWT_PUBLIC_KEY_PATH"):
            if os.environ.get(var) and not os.path.exists(os.environ[var]):
                del os.environ[var]
        import importlib

        for mod in ("gateway.config", "gateway.auth.jwt_utils"):
            sys.modules.pop(mod, None)
        import gateway.config as cfg

        importlib.reload(cfg)

        from gateway.main import app
        from gateway.db.models import Base, DenialLog, RequestLog, User
        from gateway.routes import query_routes as qr

        self.qr = qr
        self.DenialLog = DenialLog
        self.RequestLog = RequestLog

        # SQLite cannot bind uuid.UUID to a native UUID column; swap the shared
        # metadata's UUID types for CHAR(36) for this test only and restore.
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

        # StaticPool + check_same_thread=False: `ask` is a sync route, so
        # Starlette runs it in an anyio worker thread.
        engine = create_engine(
            "sqlite:///:memory:",
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
        Base.metadata.create_all(engine)
        self.session = sessionmaker(bind=engine)()
        self.addCleanup(self.session.close)

        # Engineering user at clearance 2: the clearance half of the predicate
        # is satisfied for the Finance doc (level 2), so the denial under test
        # can only come from the department half — DEPARTMENT_MISMATCH.
        self.user = User(
            username="eng2", password_hash="x", department="Engineering",
            clearance_level=2, role="employee",
        )
        self.session.add(self.user)
        self.session.commit()

        app.dependency_overrides[qr.get_db] = lambda: self.session
        app.dependency_overrides[qr.get_current_user] = lambda: self.user
        self.addCleanup(app.dependency_overrides.clear)
        self.app = app

    def _fake_chromadb(self):
        """Fake chromadb module whose collection returns the poisoned chunk.

        Patched into sys.modules so the REAL VectorStore.__init__/query run:
        the secondary check under test is the production code path
        (gateway/rag/vector_store.py), not a reimplementation.
        """
        fake_collection = mock.Mock()
        fake_collection.query = mock.Mock(return_value={
            "documents": [[SECRET_DOC_TEXT]],
            "metadatas": [[dict(FINANCE_METADATA)]],
        })
        fake_client = mock.Mock()
        fake_client.get_collection = mock.Mock(return_value=fake_collection)
        fake_module = types.ModuleType("chromadb")
        fake_module.HttpClient = mock.Mock(return_value=fake_client)
        return fake_module, fake_collection, fake_client

    def test_poisoned_finance_chunk_is_denied_and_audited(self):
        from gateway.authz.policy import build_chroma_filter

        fake_module, fake_collection, fake_client = self._fake_chromadb()

        # Embedding model is faked (no model load); OllamaClient is faked so
        # the test can prove generation is never reached after the denial.
        encode = mock.Mock(return_value=mock.Mock(tolist=mock.Mock(return_value=[0.0])))
        ollama_instance = mock.Mock(chat=mock.Mock(return_value={
            "response": "SHOULD-NOT-BE-GENERATED",
            "prompt_eval_count": 1, "eval_count": 1,
        }))

        with mock.patch.dict(sys.modules, {"chromadb": fake_module}), \
             mock.patch.object(self.qr, "get_embedding_model",
                               return_value=mock.Mock(encode=encode)), \
             mock.patch.object(self.qr, "OllamaClient",
                               return_value=ollama_instance):
            import urllib.parse

            prompt = "What is the quarterly budget outlook?"
            qs = urllib.parse.urlencode({"message": prompt})
            resp = call_asgi(self.app, "/ask", qs)

        # 1. The secondary check denied the document with the expected reason.
        #    (AuthorizationDeniedError is raised inside the real
        #    VectorStore.query after authorize() re-checks the metadata; the
        #    route maps it to HTTP 403 "Access denied".)
        self.assertEqual(resp.status_code, 403, resp.text)
        self.assertIn("Access denied", resp.text)
        request_id = _uuid.UUID(resp.headers["x-request-id"])

        # The real filter was built and sent to the (fake) collection — the
        # denial did not come from skipping the filter but from the chunk
        # violating it after retrieval.
        sent_kwargs = fake_collection.query.call_args.kwargs
        self.assertEqual(sent_kwargs["where"], build_chroma_filter(self.user))
        fake_client.get_collection.assert_called_once()

        # 2. Exactly one DenialLog row with request id, the authenticated
        #    user's attributes, reason code and sanitized prompt.
        rows = self.session.query(self.DenialLog).filter(
            self.DenialLog.request_id == request_id).all()
        self.assertEqual(len(rows), 1, "exactly one denial_logs row")
        row = rows[0]
        self.assertEqual(row.reason_code, "DEPARTMENT_MISMATCH")
        self.assertEqual(row.reason_detail, "Retrieved document failed authorization")
        self.assertEqual(row.user_id, self.user.id)
        self.assertEqual(row.department, self.user.department)
        self.assertEqual(row.clearance_level, self.user.clearance_level)
        self.assertEqual(row.sanitized_prompt, prompt)

        # 3. The retrieved document text is stored nowhere and never returned.
        audit_blob = " ".join(
            str(getattr(row, col)) for col in
            ("sanitized_prompt", "reason_detail", "reason_code")
        )
        self.assertNotIn(SECRET_DOC_TEXT, audit_blob)
        self.assertNotIn(SECRET_DOC_TEXT, resp.text)
        self.assertNotIn("finance_guidelines.txt", resp.text)

        # 4. The embedding-to-LLM generation path was never invoked, and no
        #    success (request_logs) row was written for this request.
        ollama_instance.chat.assert_not_called()
        success_rows = self.session.query(self.RequestLog).filter(
            self.RequestLog.request_id == request_id).all()
        self.assertEqual(success_rows, [], "no request_logs row on denial")


if __name__ == "__main__":
    unittest.main(verbosity=2)
