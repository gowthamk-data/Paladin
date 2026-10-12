import glob
import hashlib
import json
import os
from typing import Any

from gateway.authz.policy import DocumentAttributes
from gateway.config import (
    CHROMADB_HOST,
    CHROMADB_PORT,
    CHROMA_COLLECTION,
    EMBEDDING_MODEL,
)

CHUNK_MAX_CHARS = 500
STAGING_COLLECTION = f"{CHROMA_COLLECTION}_staging"


class ManifestError(ValueError):
    """The document set or its explicit ABAC manifest is invalid."""


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _validate_digest(value: Any) -> str:
    """Require a well-formed lowercase hex SHA-256 digest; fail closed otherwise."""
    if not isinstance(value, str) or len(value) != 64:
        raise ManifestError("manifest entry has invalid content_sha256")
    if any(char not in "0123456789abcdef" for char in value):
        raise ManifestError("manifest entry has invalid content_sha256")
    return value


class DocumentIngestor:
    def __init__(self, documents_dir: str | None = None, manifest_path: str | None = None):
        self.documents_dir = documents_dir or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "documents"
        )
        self.manifest_path = manifest_path or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "manifest.json"
        )
        self.manifest = self._load_manifest()
        # Verified document byte snapshots, keyed by manifest filename.
        # Populated (and fully replaced) by _files() on every ingestion
        # attempt so _process_file() consumes exactly the bytes that were
        # hash-verified, with no re-read from disk in between.
        self._verified_bytes: dict[str, bytes] = {}
        import chromadb
        from sentence_transformers import SentenceTransformer

        self.model = SentenceTransformer(EMBEDDING_MODEL)
        self.client = chromadb.HttpClient(host=CHROMADB_HOST, port=CHROMADB_PORT)

    def _load_manifest(self) -> dict[str, DocumentAttributes]:
        try:
            with open(self.manifest_path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise ManifestError("manifest could not be read") from exc
        if not isinstance(raw, dict) or not raw:
            raise ManifestError("manifest must be a non-empty object")
        parsed: dict[str, DocumentAttributes] = {}
        digests: dict[str, str] = {}
        for filename, attributes in raw.items():
            if not isinstance(filename, str) or not isinstance(attributes, dict):
                raise ManifestError("manifest entry is invalid")
            if "content_sha256" not in attributes:
                raise ManifestError("manifest entry is missing content_sha256")
            digests[filename] = _validate_digest(attributes["content_sha256"])
            try:
                document = DocumentAttributes(
                    **{k: v for k, v in attributes.items() if k != "content_sha256"}
                )
            except TypeError as exc:
                raise ManifestError("manifest entry has invalid fields") from exc
            if (
                type(document.classification_level) is not int
                or document.classification_level not in {1, 2, 3}
                or not isinstance(document.department, str)
                or not document.department.strip()
                or document.visibility not in {"company-wide", "department"}
            ):
                raise ManifestError("manifest entry has invalid attributes")
            parsed[filename] = document
        self.digests = digests
        return parsed

    def _files(self) -> dict[str, bytes]:
        # State isolation: explicitly clear and fully replace the verified
        # snapshots on every attempt so bytes from a prior attempt can never
        # leak into this one.
        self._verified_bytes = {}
        files = sorted(glob.glob(os.path.join(self.documents_dir, "*.*")))
        names = {os.path.basename(path) for path in files}
        if names != self.manifest.keys():
            raise ManifestError("document is missing from manifest")
        verified: dict[str, bytes] = {}
        for path in files:
            name = os.path.basename(path)
            with open(path, "rb") as handle:
                data = handle.read()
            if _sha256_hex(data) != self.digests.get(name):
                raise ManifestError(f"document content does not match manifest digest: {name}")
            verified[name] = data
        self._verified_bytes = verified
        return verified

    def ingest_directory(self) -> int:
        verified = self._files()
        staging = self.client.get_or_create_collection(
            name=STAGING_COLLECTION, metadata={"hnsw:space": "cosine"}
        )
        existing = staging.get()
        if existing.get("ids"):
            staging.delete(ids=existing["ids"])

        total_chunks = 0
        for filename in sorted(verified):
            total_chunks += self._process_file(staging, filename, verified[filename])
        self._validate_collection(staging, total_chunks)
        self._promote(staging)
        return total_chunks

    def _process_file(self, collection: Any, filename: str, data: bytes) -> int:
        attributes = self.manifest[filename]
        # Consume only the hash-verified snapshot passed in by _files();
        # the source file is never re-read here, closing the TOCTOU gap.
        content = data.decode("utf-8")
        chunks = (
            self._chunk_markdown(content)
            if os.path.splitext(filename)[1].lower() == ".md"
            else self._chunk_text(content)
        )
        for index, chunk_text in enumerate(chunks):
            collection.upsert(
                ids=[f"{filename}::chunk-{index}"],
                embeddings=[self.model.encode(chunk_text).tolist()],
                documents=[chunk_text],
                metadatas=[
                    {
                        "source_document": filename,
                        "chunk_index": index,
                        "classification_level": attributes.classification_level,
                        "department": attributes.department,
                        "visibility": attributes.visibility,
                    }
                ],
            )
        return len(chunks)

    def _validate_collection(self, collection: Any, expected_chunks: int) -> None:
        data = collection.get()
        if len(data.get("ids", [])) != expected_chunks:
            raise ManifestError("staging collection has an unexpected chunk count")
        for metadata in data.get("metadatas", []):
            if not metadata or metadata.get("source_document") not in self.manifest:
                raise ManifestError("staging collection has unmanifested metadata")
            attributes = self.manifest[metadata["source_document"]]
            if (
                metadata.get("classification_level") != attributes.classification_level
                or metadata.get("department") != attributes.department
                or metadata.get("visibility") != attributes.visibility
            ):
                raise ManifestError("staging collection metadata does not match manifest")

    def _promote(self, staging: Any) -> None:
        try:
            active = self.client.get_collection(name=CHROMA_COLLECTION)
            existing = active.get()
            if existing.get("ids"):
                active.delete(ids=existing["ids"])
        except Exception:
            active = self.client.create_collection(
                name=CHROMA_COLLECTION, metadata={"hnsw:space": "cosine"}
            )
        data = staging.get(include=["embeddings", "documents", "metadatas"])
        if data.get("ids"):
            active.upsert(
                ids=data["ids"],
                embeddings=data["embeddings"],
                documents=data["documents"],
                metadatas=data["metadatas"],
            )

    @staticmethod
    def _chunk_text(text: str, max_chars: int = CHUNK_MAX_CHARS) -> list[str]:
        words, chunks, current = text.split(), [], []
        for word in words:
            current.append(word)
            if len(" ".join(current)) >= max_chars:
                chunks.append(" ".join(current))
                current = []
        return chunks + ([" ".join(current)] if current else []) or [text]

    @staticmethod
    def _chunk_markdown(text: str, max_chars: int = CHUNK_MAX_CHARS) -> list[str]:
        paragraphs, chunks, current = text.split("\n\n"), [], []
        for paragraph in paragraphs:
            current.append(paragraph)
            if len("\n\n".join(current)) >= max_chars:
                chunks.append("\n\n".join(current))
                current = []
        return chunks + (["\n\n".join(current)] if current else []) or [text]


if __name__ == "__main__":
    DocumentIngestor().ingest_directory()
