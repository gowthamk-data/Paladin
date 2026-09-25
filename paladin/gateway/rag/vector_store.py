from gateway.config import CHROMADB_HOST, CHROMADB_PORT
from gateway.errors import ServiceUnavailableError


class VectorStore:
    def __init__(self):
        import chromadb

        try:
            self.client = chromadb.HttpClient(host=CHROMADB_HOST, port=CHROMADB_PORT)
            self.collection = self.client.get_or_create_collection(
                name="paladin_docs",
                metadata={"hnsw:space": "cosine"},
            )
        except ServiceUnavailableError:
            raise
        except Exception as exc:
            raise ServiceUnavailableError("vector store") from exc

    def query(self, embedding, user_clearance, n_results=5):
        try:
            return self.collection.query(
                query_embeddings=[embedding],
                n_results=n_results,
                where={"clearance_level": {"$lte": user_clearance}},
            )
        except ServiceUnavailableError:
            raise
        except Exception as exc:
            raise ServiceUnavailableError("vector store") from exc
