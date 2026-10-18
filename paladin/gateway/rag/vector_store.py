from typing import Any

from gateway.authz.policy import UserAttributes, authorize, build_chroma_filter
from gateway.config import CHROMADB_HOST, CHROMADB_PORT, CHROMA_COLLECTION
from gateway.errors import AuthorizationDeniedError, ServiceUnavailableError
from gateway.rag.generations import read_active_generation


class VectorStore:
    """Read-only handle onto exactly one active generation collection.

    Construction (once per /ask request) resolves the authoritative
    active-generation pointer in PostgreSQL and opens that generation's
    collection. A generation is only ever pointed at after it has been fully
    built and validated by ingestion, so a request never sees a mix of old
    and new rows; a request that resolved before a cutover finishes against
    the prior generation, which ingestion retains for rollback. With no
    pointer row yet (pre-versioning deployment), the legacy collection name
    is served; with the pointer store or the collection unreachable, the
    store fails closed as unavailable rather than guessing.
    """

    def __init__(self, db=None, collection_name: str = CHROMA_COLLECTION):
        """Bind to the request's DB session first, collection name second.

        The session is positional-first on purpose: ``query_routes.py`` calls
        ``VectorStore(db)``, and an earlier signature with ``collection_name``
        first silently bound the Session as the collection name. Keeping the
        legacy fallback as an explicit keyword preserves the old behaviour
        for any non-route caller.
        """
        import chromadb

        try:
            self.client = chromadb.HttpClient(host=CHROMADB_HOST, port=CHROMADB_PORT)
            self.generation = read_active_generation(db) or collection_name
            self.collection = self.client.get_collection(name=self.generation)
        except ServiceUnavailableError:
            raise
        except Exception as exc:
            raise ServiceUnavailableError("vector store") from exc

    def query(self, embedding: list[float], user: UserAttributes, n_results: int = 5):
        try:
            where = build_chroma_filter(user)
            results = self.collection.query(
                query_embeddings=[embedding],
                n_results=n_results,
                where=where,
            )
            documents = results.get("documents") or [[]]
            metadatas = results.get("metadatas") or [[]]
            if len(documents) != len(metadatas):
                raise ValueError("vector store returned mismatched result shapes")
            safe_documents: list[str] = []
            safe_metadatas: list[dict[str, Any]] = []
            for document, metadata in zip(documents[0], metadatas[0]):
                decision = authorize("read", user, metadata)
                if not decision.allowed:
                    raise AuthorizationDeniedError(decision.reason_code)
                if not isinstance(document, str) or not isinstance(metadata, dict):
                    raise ValueError("vector store returned invalid result")
                safe_documents.append(document)
                safe_metadatas.append(metadata)
            results["documents"] = [safe_documents]
            results["metadatas"] = [safe_metadatas]
            return results
        except ServiceUnavailableError:
            raise
        except AuthorizationDeniedError:
            raise
        except Exception as exc:
            raise ServiceUnavailableError("vector store") from exc
