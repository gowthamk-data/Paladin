import os
import glob
from sentence_transformers import SentenceTransformer
import chromadb
from gateway.config import CHROMADB_HOST, CHROMADB_PORT, EMBEDDING_MODEL, EMBEDDING_DIMENSION

CHUNK_MAX_TOKENS = 512
CHUNK_OVERLAP = 0.5


class DocumentIngestor:
    def __init__(self, documents_dir: str = None):
        if documents_dir is None:
            self.documents_dir = os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "documents"
            )
        else:
            self.documents_dir = documents_dir
        self.model = SentenceTransformer(EMBEDDING_MODEL)
        self.client = chromadb.HttpClient(host=CHROMADB_HOST, port=CHROMADB_PORT)
        self.collection = self.client.get_or_create_collection(
            name="paladin_docs",
            metadata={"hnsw:space": "cosine"},
        )

    def ingest_directory(self):
        files = sorted(glob.glob(os.path.join(self.documents_dir, "*.*")))
        total_chunks = 0
        for filepath in files:
            chunks = self._process_file(filepath)
            total_chunks += len(chunks)
            print(f"[INGEST] {os.path.basename(filepath)}: {len(chunks)} chunks")
        print(f"[INGEST] Total: {total_chunks} chunks ingested.")
        return total_chunks

    def _process_file(self, filepath: str):
        ext = os.path.splitext(filepath)[1].lower()
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()

        if ext == ".md":
            chunks = self._chunk_markdown(content, filepath)
        else:
            chunks = self._chunk_text(content, filepath)

        for i, chunk_text in enumerate(chunks):
            embedding = self.model.encode(chunk_text).tolist()
            clearance = self._determine_clearance(filepath, chunk_text)
            self.collection.add(
                ids=[f"{os.path.basename(filepath)}::chunk-{i}"],
                embeddings=[embedding],
                documents=[chunk_text],
                metadatas=[
                    {
                        "source_document": os.path.basename(filepath),
                        "chunk_index": i,
                        "clearance_level": clearance,
                        "department": self._determine_department(filepath),
                    }
                ],
            )
        return chunks

    def _chunk_text(self, text: str, filepath: str, max_chars: int = 500) -> list:
        words = text.split()
        chunks = []
        current = []
        for word in words:
            current.append(word)
            if len(" ".join(current)) >= max_chars:
                chunks.append(" ".join(current))
                current = []
        if current:
            chunks.append(" ".join(current))
        return chunks if chunks else [text]

    def _chunk_markdown(self, text: str, filepath: str, max_chars: int = 500) -> list:
        paragraphs = text.split("\n\n")
        chunks = []
        current = []
        for para in paragraphs:
            current.append(para)
            if len("\n\n".join(current)) >= max_chars:
                chunks.append("\n\n".join(current))
                current = []
        if current:
            chunks.append("\n\n".join(current))
        return chunks if chunks else [text]

    def _determine_clearance(self, filepath: str, chunk_text: str) -> int:
        filename = os.path.basename(filepath).lower()
        if "executive" in filename:
            return 3
        if "finance" in filename or "hr" in filename:
            return 2
        return 1

    def _determine_department(self, filepath: str) -> str:
        filename = os.path.basename(filepath).lower()
        if "hr" in filename:
            return "HR"
        if "finance" in filename:
            return "Finance"
        if "executive" in filename:
            return "Executive"
        return "Engineering"


if __name__ == "__main__":
    ingestor = DocumentIngestor()
    ingestor.ingest_directory()