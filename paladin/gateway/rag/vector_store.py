from typing import Any

from gateway.authz.policy import UserAttributes, authorize, build_chroma_filter
from gateway.config import CHROMADB_HOST, CHROMADB_PORT, CHROMA_COLLECTION
from gateway.errors import AuthorizationDeniedError, ServiceUnavailableError


class VectorStore:
    def __init__(self, collection_name: str = CHROMA_COLLECTION):
        import chromadb

        try:
            self.client = chromadb.HttpClient(host=CHROMADB_HOST, port=CHROMADB_PORT)
            self.collection = self.client.get_collection(name=collection_name)
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
