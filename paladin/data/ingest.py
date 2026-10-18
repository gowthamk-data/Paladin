import argparse
import glob
import hashlib
import json
import os
import re
from typing import Any

from gateway.authz.policy import DocumentAttributes
from gateway.config import (
    CHROMADB_HOST,
    CHROMADB_PORT,
    CHROMA_COLLECTION,
    EMBEDDING_MODEL,
)
from gateway.rag.generations import GenerationError, GenerationStore

CHUNK_MAX_CHARS = 500

# How long a deactivated generation's collection is retained after it loses
# the pointer, even though it is outside the {active, rollback target, legacy}
# retention set. The gateway resolves the pointer once per /ask request and
# then embeds, queries Chroma and calls Ollama, so a request that opened the
# previous generation just before a cutover can still be reading it. The value
# is deliberately far above the slowest single request
# (OLLAMA_TIMEOUT_SECONDS, 300s by default) while still bounding how long a
# superseded generation lingers.
IN_USE_GRACE_SECONDS = float(
    os.environ.get("INGEST_IN_USE_GRACE_SECONDS", "900")
)


class ManifestError(ValueError):
    """The document set or its explicit ABAC manifest is invalid."""


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# chromadb 0.4.24 (the pinned version) signals "absent" and "unreachable" with
# two different driver errors, and NOT with the ones you would guess:
#
#   * a missing collection arrives as a plain ``Exception`` carrying the
#     serialised server error, i.e. the message text
#     ``{"error":"ValueError('Collection paladin_docs_abac does not exist.')"}``;
#   * an unreachable server raises ``ValueError("Could not connect to a Chroma
#     server. Are you sure it is running?")``.
#
# So the two cases must be told apart by driver-error *signature* — the exact
# "Collection <name> does not exist" phrase versus known connect-failure text —
# never by a bare "does not exist" substring and never by exception type alone.
# Getting this wrong fails open: treating an unreachable Chroma as absent would
# make a deployment adopt a nonexistent legacy collection, and treating absent
# as unreachable would crash every fresh install.
_MISSING_COLLECTION_RE = re.compile(
    r"collection\s+['\"]?(?P<name>[^\s'\"]+)['\"]?\s+does\s+not\s+exist",
    re.IGNORECASE,
)
# Connection-level failures. These must never be read as "collection absent".
_UNREACHABLE_MARKERS = (
    "could not connect to a chroma server",
    "connection refused",
    "max retries exceeded",
    "failed to establish a new connection",
    "name or service not known",
    "temporary failure in name resolution",
)


def _is_missing_collection(exc: BaseException, name: str | None = None) -> bool:
    """True only for chromadb's verified "this collection is absent" error.

    ``name``, when given, must equal the name in the error text, so an error
    about some *other* collection is never taken as evidence about this one.
    Any other driver error — an unreachable server, an authentication failure,
    a 500 — returns False, which callers treat as "fail closed" rather than as
    "fresh install".
    """
    message = str(exc).lower()
    if any(marker in message for marker in _UNREACHABLE_MARKERS):
        return False
    match = _MISSING_COLLECTION_RE.search(message)
    if match is None:
        return False
    return name is None or match.group("name") == name.lower()


def _probe_collection_exists(client: Any, name: str) -> bool:
    """Probe for a Chroma collection without creating it.

    Only chromadb's verified missing-collection error counts as absent; every
    other error propagates so a half-dead Chroma fails closed instead of
    looking like a fresh install.
    """
    try:
        client.get_collection(name=name)
    except Exception as exc:
        if _is_missing_collection(exc, name):
            return False
        raise
    return True


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
        # Authoritative active-generation pointer (PostgreSQL-backed). The
        # probe keeps bootstrap and rollback honest: the legacy collection is
        # only adopted when it truly exists in Chroma (fresh installs start
        # with no pointer row), and rollback refuses a target whose
        # collection is gone. Absence is identified by chromadb's exact
        # "Collection <name> does not exist" driver error, which in 0.4.24
        # arrives as a *plain Exception* wrapping the serialised server
        # error — not as ValueError (that is the unreachable-server shape).
        # Any other client error propagates so a half-dead Chroma cannot be
        # mistaken for an empty one.
        self.store = GenerationStore(
            collection_exists=self._chroma_collection_exists
        )
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
        """Build, validate and activate a fresh generation of the vector index.

        Cutover is a pointer flip, not a copy: the new generation collection
        is invisible to queries until the authoritative pointer row switches
        to it in a single PostgreSQL transaction. Any failure before that
        flip leaves the previous generation serving untouched; the half-built
        generation is discarded (best effort) so a failed run leaves no
        partial state behind.
        """
        verified = self._files()
        # Takes the ingestion advisory lock, creates the pointer table if
        # needed and adopts the legacy collection as the active generation on
        # first use; concurrent ingestions are refused with GenerationError.
        self.store.acquire()
        generation_name: str | None = None
        activated = False
        try:
            generation_name = self.store.next_generation_name()
            self.store.register(generation_name)
            collection = self.client.create_collection(
                name=generation_name, metadata={"hnsw:space": "cosine"}
            )
            total_chunks = 0
            for filename in sorted(verified):
                total_chunks += self._process_file(
                    collection, filename, verified[filename]
                )
            if total_chunks == 0:
                raise ManifestError(
                    "no chunks produced; refusing to activate an empty generation"
                )
            self._validate_collection(collection, total_chunks)
            # The single atomic cutover: one transaction demotes the current
            # active generation and activates the fully built new one.
            self.store.activate(generation_name)
            activated = True
            # Post-cutover retention: never fatal. Orphans (older generations
            # and leftovers of crashed runs) are removed; the active
            # generation, the rollback target and the legacy bootstrap
            # collection are kept.
            cleanup_generations(self.client, self.store)
            return total_chunks
        except BaseException:
            if generation_name and not activated:
                self._discard_generation(generation_name)
            raise
        finally:
            self.store.release()

    def _discard_generation(self, generation_name: str) -> None:
        """Best-effort removal of this run's un-activated generation.

        Never masks the original failure: both the Chroma deletion and the
        row deletion swallow their own errors; anything left behind is an
        invisible orphan that the next run's cleanup removes.
        """
        try:
            self.client.delete_collection(name=generation_name)
        except Exception:
            pass
        try:
            self.store.forget(generation_name)
        except Exception:
            pass

    def _chroma_collection_exists(self, name: str) -> bool:
        return _probe_collection_exists(self.client, name)

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
            raise ManifestError("generation collection has an unexpected chunk count")
        for metadata in data.get("metadatas", []):
            if not metadata or metadata.get("source_document") not in self.manifest:
                raise ManifestError("generation collection has unmanifested metadata")
            attributes = self.manifest[metadata["source_document"]]
            if (
                metadata.get("classification_level") != attributes.classification_level
                or metadata.get("department") != attributes.department
                or metadata.get("visibility") != attributes.visibility
            ):
                raise ManifestError(
                    "generation collection metadata does not match manifest"
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


def cleanup_generations(
    client: Any,
    store: GenerationStore,
    in_use_grace_seconds: float = IN_USE_GRACE_SECONDS,
) -> list[str]:
    """Retention pass: delete collections outside the retention set.

    Retention: the active generation, the rollback target (previous
    generation), and the legacy bootstrap collection are never touched, and
    neither is any generation deactivated less than
    ``in_use_grace_seconds`` ago — the gateway resolves the pointer per
    request, so an in-flight request can still hold a generation that lost the
    pointer moments ago and deleting its collection would break it. Everything
    else — older generations and the invisible leftovers of crashed runs — has
    its Chroma collection deleted first, then its pointer row; a failed
    deletion skips the row so the next run retries, and a row whose collection
    is already gone is dropped so it cannot wedge retention forever. Never
    raises: cleanup is best-effort and retried on every run. Callers must hold
    the ingestion advisory lock.
    """
    removed: list[str] = []
    for name in store.cleanup_candidates(in_use_grace_seconds=in_use_grace_seconds):
        try:
            client.delete_collection(name=name)
        except Exception as exc:
            # Collection already gone (e.g. a prior partial cleanup): drop the
            # dangling row. Identified by driver-error signature, not by a bare
            # "does not exist" substring — an unreachable Chroma is not proof
            # of absence, so it keeps the row for the next run.
            if not _is_missing_collection(exc, name):
                continue
        try:
            store.forget(name)
        except Exception:
            continue
        removed.append(name)
    return removed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Paladin document ingestion")
    parser.add_argument(
        "--rollback",
        action="store_true",
        help="flip the pointer back to the previous generation",
    )
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help="run retention cleanup of out-of-retention generations only",
    )
    args = parser.parse_args(argv)

    if args.rollback and args.cleanup:
        parser.error("--rollback and --cleanup are mutually exclusive")

    try:
        if args.rollback:
            import chromadb

            client = chromadb.HttpClient(host=CHROMADB_HOST, port=CHROMADB_PORT)

            def _exists(name: str) -> bool:
                return _probe_collection_exists(client, name)

            store = GenerationStore(collection_exists=_exists)
            store.acquire()
            try:
                target = store.rollback()
            finally:
                store.release()
            print(f"rolled back to {target}")
            return 0
        if args.cleanup:
            store = GenerationStore()
            store.acquire()
            try:
                import chromadb

                client = chromadb.HttpClient(host=CHROMADB_HOST, port=CHROMADB_PORT)
                removed = cleanup_generations(client, store)
            finally:
                store.release()
            print(f"cleanup removed {len(removed)} generation(s)")
            for name in removed:
                print(f"  removed {name}")
            return 0
        total_chunks = DocumentIngestor().ingest_directory()
        print(f"ingested {total_chunks} chunks into the active generation")
        return 0
    except (GenerationError, ManifestError) as exc:
        print(f"ingestion refused: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
