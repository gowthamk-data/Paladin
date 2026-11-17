"""PostgreSQL-backed /ask regression for a pre-generation deployment.

The test requires a throwaway PostgreSQL service reachable as
``gate2-fallback-pg`` on a private test network. It creates and removes its own
random schema, deliberately omits ``vector_generations``, and checks that a
real ``/ask`` request can use the legacy Chroma collection and commit its
``request_logs`` audit row. Chroma and Ollama are faked; PostgreSQL, the route,
pointer resolution, and audit persistence are real.

This does not exercise the live Paladin database or claim that semantic
retrieval/LLM generation was tested.
"""

import os
import sys
import types
import unittest
import uuid
from unittest import mock

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

try:
    import fastapi  # noqa: F401
    import slowapi  # noqa: F401
    import sqlalchemy as sa
    from sqlalchemy.engine import make_url
    from sqlalchemy.orm import sessionmaker

    _GATEWAY_DEPS = True
except ImportError:
    _GATEWAY_DEPS = False

_PG_URL = os.environ.get("PALADIN_PG_FALLBACK_URL")
_CHROMA_HOST = os.environ.get("PALADIN_CHROMA_FALLBACK_HOST")


@unittest.skipUnless(
    _GATEWAY_DEPS and _PG_URL,
    "set PALADIN_PG_FALLBACK_URL to the private gate2-fallback-pg service",
)
class AskLegacyPointerFallbackPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        parsed = make_url(_PG_URL)
        if parsed.host != "gate2-fallback-pg":
            raise RuntimeError(
                "refusing PostgreSQL fallback test unless the database host is "
                "the private gate2-fallback-pg service"
            )

        cls.schema = f"fallback_{uuid.uuid4().hex[:12]}"
        cls.root_engine = sa.create_engine(_PG_URL)
        with cls.root_engine.begin() as connection:
            connection.execute(sa.text(f'CREATE SCHEMA "{cls.schema}"'))

        scoped_url = parsed.update_query_dict(
            {"options": f"-csearch_path={cls.schema}"}
        )
        cls.engine = sa.create_engine(scoped_url)

        from gateway.db.models import Base, RequestLog, User

        # Deliberately create only the route's required tables. In particular,
        # GenerationRow/vector_generations is absent to reproduce the rollout
        # bootstrap condition.
        Base.metadata.create_all(
            bind=cls.engine,
            tables=[User.__table__, RequestLog.__table__],
        )
        cls.Session = sessionmaker(bind=cls.engine, expire_on_commit=False)
        cls.setup_session = cls.Session()
        from gateway.db.seed import SEED_USERS

        cls.users = {}
        for user_data in SEED_USERS:
            user = User(
                username=user_data["username"],
                password_hash="test-only",
                department=user_data["department"],
                clearance_level=user_data["clearance_level"],
                role=user_data["role"],
            )
            cls.setup_session.add(user)
            cls.users[user.username] = user
        cls.setup_session.commit()
        cls.user = cls.users["emp1"]

        from gateway.main import app
        from gateway.routes import query_routes as qr

        cls.app = app
        cls.qr = qr

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "app"):
            cls.app.dependency_overrides.clear()
        if hasattr(cls, "setup_session"):
            cls.setup_session.close()
        if hasattr(cls, "engine"):
            cls.engine.dispose()
        if hasattr(cls, "root_engine"):
            with cls.root_engine.begin() as connection:
                connection.execute(sa.text(f'DROP SCHEMA IF EXISTS "{cls.schema}" CASCADE'))
            cls.root_engine.dispose()

    def test_legacy_fallback_keeps_postgres_session_usable_for_audit(self):
        from gateway.db.models import GenerationRow, RequestLog
        from tests.test_ask_http_503_live import call_asgi

        self.assertFalse(
            sa.inspect(self.engine).has_table(GenerationRow.__tablename__),
            "fixture must have no generation pointer table",
        )

        opened_sessions = []

        def db_override():
            db = self.Session()
            opened_sessions.append(db)
            try:
                yield db
            finally:
                db.close()

        self.app.dependency_overrides[self.qr.get_db] = db_override
        self.app.dependency_overrides[self.qr.get_current_user] = lambda: self.user

        returned_collections = []
        fake_collection = mock.Mock()
        fake_collection.query.return_value = {
            "documents": [["safe synthetic engineering guidance"]],
            "metadatas": [[{
                "source_document": "engineering_policy.txt",
                "classification_level": 1,
                "department": "Engineering",
                "visibility": "department",
            }]],
        }
        fake_client = mock.Mock()

        def get_collection(name, **kwargs):
            returned_collections.append(name)
            return fake_collection

        fake_client.get_collection.side_effect = get_collection
        fake_chromadb = types.ModuleType("chromadb")
        fake_chromadb.HttpClient = mock.Mock(return_value=fake_client)
        fake_model = mock.Mock(encode=mock.Mock(
            return_value=mock.Mock(tolist=mock.Mock(return_value=[0.1] * 384))
        ))
        fake_ollama = mock.Mock(chat=mock.Mock(return_value={
            "response": "Synthetic answer",
            "prompt_eval_count": 4,
            "eval_count": 3,
        }))

        try:
            with mock.patch.dict(sys.modules, {"chromadb": fake_chromadb}), \
                 mock.patch.object(self.qr, "get_embedding_model", return_value=fake_model), \
                 mock.patch.object(self.qr, "OllamaClient", return_value=fake_ollama):
                from urllib.parse import urlencode

                response = call_asgi(
                    self.app,
                    "/ask",
                    urlencode({"message": "Summarize the engineering policy"}),
                )
        finally:
            self.app.dependency_overrides.clear()

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["response"], "Synthetic answer")
        self.assertEqual(len(returned_collections), 1)

        from gateway.config import CHROMA_COLLECTION

        self.assertEqual(returned_collections, [CHROMA_COLLECTION])
        request_id = uuid.UUID(payload["request_id"])

        # A fresh PostgreSQL session sees one committed success row; this is
        # the critical assertion that the fallback did not leave the request's
        # transaction aborted after inspecting the absent pointer table.
        with self.Session() as verify_session:
            rows = verify_session.query(RequestLog).filter(
                RequestLog.request_id == request_id
            ).all()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].status, "success")
            self.assertEqual(rows[0].user_id, self.user.id)
            self.assertEqual(
                verify_session.execute(sa.text("SELECT 1")).scalar_one(), 1
            )

        fake_ollama.chat.assert_called_once()
        self.assertEqual(len(opened_sessions), 1)

    @unittest.skipUnless(
        _CHROMA_HOST == "gate2-fallback-chroma",
        "requires the private gate2-fallback-chroma service",
    )
    def test_candidate_legacy_fallback_matrix_against_real_chroma(self):
        """Exercise all 16 source-visibility cases through the candidate route.

        PostgreSQL and Chroma are real disposable services. User auth is
        overridden, embeddings are fixed and deterministic, and Ollama is
        stubbed so this checks retrieval filtering, fallback transaction
        health, and audit commits rather than model quality or login.
        """
        import json
        from pathlib import Path

        import chromadb
        from gateway.config import CHROMA_COLLECTION, CHROMADB_HOST, CHROMADB_PORT
        from gateway.db.models import GenerationRow, RequestLog
        from tests.test_ask_http_503_live import call_asgi

        self.assertEqual(CHROMADB_HOST, "gate2-fallback-chroma")
        self.assertFalse(
            sa.inspect(self.engine).has_table(GenerationRow.__tablename__),
            "matrix must exercise the pre-generation missing-pointer path",
        )

        manifest_path = Path(PROJECT_ROOT) / "data" / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        client = chromadb.HttpClient(host=CHROMADB_HOST, port=CHROMADB_PORT)
        collection = client.create_collection(
            name=CHROMA_COLLECTION,
            metadata={"hnsw:space": "cosine"},
        )
        files = list(manifest)
        collection.add(
            ids=files,
            documents=[f"Synthetic fixture content for {name}" for name in files],
            embeddings=[[1.0] + [0.0] * 383 for _ in files],
            metadatas=[
                {
                    "source_document": name,
                    "classification_level": manifest[name]["classification_level"],
                    "department": manifest[name]["department"],
                    "visibility": manifest[name]["visibility"],
                }
                for name in files
            ],
        )

        active_user = [self.users["emp1"]]
        self.app.dependency_overrides[self.qr.get_db] = self._db_override
        self.app.dependency_overrides[self.qr.get_current_user] = lambda: active_user[0]

        model = mock.Mock(encode=mock.Mock(
            return_value=mock.Mock(tolist=mock.Mock(return_value=[1.0] + [0.0] * 383))
        ))
        ollama = mock.Mock(chat=mock.Mock(return_value={
            "response": "Synthetic test response",
            "prompt_eval_count": 4,
            "eval_count": 2,
        }))
        expected = {
            "emp1": {
                "engineering_policy.txt": True,
                "finance_guidelines.txt": False,
                "executive_briefing.md": False,
                "hr_handbook.md": False,
            },
            "mgr1": {
                "engineering_policy.txt": True,
                "finance_guidelines.txt": False,
                "executive_briefing.md": False,
                "hr_handbook.md": True,
            },
            "admin1": {
                "engineering_policy.txt": False,
                "finance_guidelines.txt": False,
                "executive_briefing.md": True,
                "hr_handbook.md": True,
            },
            "fin1": {
                "engineering_policy.txt": False,
                "finance_guidelines.txt": True,
                "executive_briefing.md": False,
                "hr_handbook.md": True,
            },
        }

        outcomes = []
        try:
            with mock.patch.object(self.qr, "get_embedding_model", return_value=model), \
                 mock.patch.object(self.qr, "OllamaClient", return_value=ollama):
                from urllib.parse import urlencode

                for username, document_expectations in expected.items():
                    active_user[0] = self.users[username]
                    for filename, allowed in document_expectations.items():
                        response = call_asgi(
                            self.app,
                            "/ask",
                            urlencode({"message": f"Check synthetic access for {filename}"}),
                        )
                        self.assertEqual(response.status_code, 200, response.text)
                        sources = response.json()["sources"]
                        found = filename in sources
                        self.assertEqual(
                            found,
                            allowed,
                            f"{username} -> {filename}: expected {allowed}, got {found}; sources={sources}",
                        )
                        outcomes.append((username, filename, allowed, found))
        finally:
            self.app.dependency_overrides.clear()
            client.delete_collection(name=CHROMA_COLLECTION)

        self.assertEqual(len(outcomes), 16)
        with self.Session() as verify_session:
            rows = verify_session.query(RequestLog).all()
            self.assertEqual(len(rows), 16)
            self.assertTrue(all(row.status == "success" for row in rows))
        ollama.chat.assert_called()

    def _db_override(self):
        db = self.Session()
        try:
            yield db
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
