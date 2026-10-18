import hashlib
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from data.ingest import DocumentIngestor, ManifestError
from tests.generation_test_support import FakeChromaClient, SQLiteGenerationStore

KNOWN_CONTENT = b"one two three"
KNOWN_SHA256 = hashlib.sha256(KNOWN_CONTENT).hexdigest()


def make_ingestor(tmp_path, manifest, content=KNOWN_CONTENT):
    documents = tmp_path / "documents"
    documents.mkdir()
    (documents / "known.txt").write_bytes(content)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    ingestor = DocumentIngestor.__new__(DocumentIngestor)
    ingestor.documents_dir = str(documents)
    ingestor.manifest_path = str(manifest_path)
    ingestor._verified_bytes = {}
    return ingestor


def valid_manifest():
    return {
        "known.txt": {
            "classification_level": 1,
            "department": "Engineering",
            "visibility": "department",
            "content_sha256": KNOWN_SHA256,
        }
    }


def test_manifest_rejects_unlisted_documents(tmp_path):
    ingestor = make_ingestor(tmp_path, valid_manifest())
    (tmp_path / "documents" / "unknown.txt").write_text("unlisted", encoding="utf-8")
    ingestor.manifest = ingestor._load_manifest()
    with pytest.raises(ManifestError):
        ingestor._files()


def test_chunk_ids_and_metadata_are_stable_for_reingestion(tmp_path):
    ingestor = make_ingestor(tmp_path, valid_manifest())
    ingestor.manifest = ingestor._load_manifest()

    class Encoded:
        def tolist(self):
            return [0.1, 0.2]

    ingestor.model = type("Model", (), {"encode": lambda self, text: Encoded()})()

    class Collection:
        def __init__(self):
            self.rows = []

        def upsert(self, **kwargs):
            self.rows.append(kwargs)

    collection = Collection()
    verified = ingestor._files()
    assert set(verified) == {"known.txt"}
    first_count = ingestor._process_file(collection, "known.txt", verified["known.txt"])
    first = list(collection.rows)
    collection.rows.clear()
    second_count = ingestor._process_file(collection, "known.txt", verified["known.txt"])
    assert first_count == second_count
    assert [row["ids"] for row in first] == [row["ids"] for row in collection.rows]
    assert first[0]["metadatas"] == collection.rows[0]["metadatas"]


# ---------------------------------------------------------------------------
# Content-hash binding (Stage 2). The digest binds the owner's approval to the
# exact document bytes; it does NOT authenticate who approved.
# ---------------------------------------------------------------------------

def test_valid_hash_is_accepted_and_files_returned(tmp_path):
    ingestor = make_ingestor(tmp_path, valid_manifest())
    ingestor.manifest = ingestor._load_manifest()
    assert ingestor.digests == {"known.txt": KNOWN_SHA256}
    verified = ingestor._files()
    assert verified == {"known.txt": KNOWN_CONTENT}
    assert ingestor._verified_bytes == {"known.txt": KNOWN_CONTENT}


def test_verified_state_is_replaced_not_accumulated(tmp_path):
    # State isolation: a failed second attempt must not leave the first
    # attempt's verified bytes behind.
    ingestor = make_ingestor(tmp_path, valid_manifest())
    ingestor.manifest = ingestor._load_manifest()
    ingestor._files()
    assert set(ingestor._verified_bytes) == {"known.txt"}
    (tmp_path / "documents" / "known.txt").write_bytes(b"tampered after verify")
    with pytest.raises(ManifestError):
        ingestor._files()
    assert ingestor._verified_bytes == {}


def test_manifest_missing_hash_is_rejected(tmp_path):
    manifest = valid_manifest()
    del manifest["known.txt"]["content_sha256"]
    ingestor = make_ingestor(tmp_path, manifest)
    with pytest.raises(ManifestError):
        ingestor._load_manifest()


@pytest.mark.parametrize(
    "bad_hash",
    [
        "",                          # empty
        "not-a-hash",                # wrong length + non-hex
        "A" * 64,                    # uppercase not accepted (strict lowercase hex)
        "g" * 64,                    # non-hex character
        KNOWN_SHA256[:-1],           # too short (63)
        KNOWN_SHA256 + "0",          # too long (65)
        12345,                       # not a string
    ],
)
def test_manifest_malformed_hash_is_rejected(tmp_path, bad_hash):
    manifest = valid_manifest()
    manifest["known.txt"]["content_sha256"] = bad_hash
    ingestor = make_ingestor(tmp_path, manifest)
    with pytest.raises(ManifestError):
        ingestor._load_manifest()


def test_document_changed_after_hash_fails_closed(tmp_path):
    ingestor = make_ingestor(tmp_path, valid_manifest())
    ingestor.manifest = ingestor._load_manifest()
    # Tamper with the document after the manifest hash was produced.
    (tmp_path / "documents" / "known.txt").write_bytes(b"one two THREE tampered")
    with pytest.raises(ManifestError):
        ingestor._files()


def test_ingested_content_cannot_differ_from_verified_snapshot(tmp_path):
    # TOCTOU regression: capture the verified snapshot, then change the file
    # on disk before processing. The ingested content must be the verified
    # snapshot, not the tampered file.
    ingestor = make_ingestor(tmp_path, valid_manifest())
    ingestor.manifest = ingestor._load_manifest()
    verified = ingestor._files()

    class Encoded:
        def tolist(self):
            return [0.1, 0.2]

    ingestor.model = type("Model", (), {"encode": lambda self, text: Encoded()})()

    class Collection:
        def __init__(self):
            self.documents = []

        def upsert(self, **kwargs):
            self.documents.extend(kwargs["documents"])

    collection = Collection()
    (tmp_path / "documents" / "known.txt").write_bytes(b"tampered after verify")
    ingestor._process_file(collection, "known.txt", verified["known.txt"])
    assert collection.documents and all(
        "tampered" not in text for text in collection.documents
    )
    assert any("one two three" in text for text in collection.documents)



def test_ingest_directory_uses_verified_snapshots_end_to_end(tmp_path):
    # Isolated end-to-end: bytes verified by _files() must be the bytes that
    # reach processing, validation, and the activated generation. Fake Chroma
    # client, SQLite pointer store, and a mock embedding model: no live stack.
    ingestor = make_ingestor(tmp_path, valid_manifest())
    ingestor.manifest = ingestor._load_manifest()

    real_process_file = ingestor._process_file

    def mutating_process_file(collection, filename, data):
        # Mutate the source file mid-run, after _files() verified it but
        # before processing consumes the snapshot. Production code never
        # re-reads the file, so tampered bytes must never reach the generation.
        (tmp_path / "documents" / filename).write_bytes(b"tampered after verify")
        return real_process_file(collection, filename, data)

    ingestor._process_file = mutating_process_file

    class Encoded:
        def tolist(self):
            return [0.1, 0.2]

    ingestor.model = type("Model", (), {"encode": lambda self, text: Encoded()})()
    client = FakeChromaClient()
    ingestor.client = client
    store = SQLiteGenerationStore(
        create_engine(
            "sqlite://",
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
    )
    ingestor.store = store

    real_validate = ingestor._validate_collection
    validate_calls = []

    def validate_spy(collection, expected_chunks):
        validate_calls.append(expected_chunks)
        return real_validate(collection, expected_chunks)

    ingestor._validate_collection = validate_spy

    total = ingestor.ingest_directory()

    # 1. Chunk count: the returned total must equal what the activated
    #    generation holds, and validation must have run exactly once.
    assert total > 0, "expected at least one chunk from the fixture document"
    store.acquire()
    try:
        active_name = store.active_name()
    finally:
        store.release()
    rows = list(client.collections[active_name].store.values())
    assert total == len(rows), (
        f"returned count {total} != activated rows {len(rows)}"
    )
    assert validate_calls == [total], (
        f"generation validation not invoked with the expected count: {validate_calls}"
    )

    # The mid-run mutation really happened, so the assertions below are
    # meaningful: bytes on disk now differ from the verified snapshot.
    assert (tmp_path / "documents" / "known.txt").read_bytes() == (
        b"tampered after verify"
    )

    # 2. Verified bytes only: tampered bytes never reached the activated
    #    generation, while the verified snapshot text did.
    assert rows, "activated generation received no chunks"
    assert all(
        "tampered" not in row["documents"] for row in rows
    ), "unverified bytes reached the activated generation"
    assert any("one two three" in row["documents"] for row in rows)

    # 3. Content check: every activated row carries the verified chunk with
    #    its embeddings and ABAC metadata (a dropped or altered row fails).
    assert len(rows) == total, (
        "activation did not carry every verified chunk"
    )
    for row in rows:
        assert row["documents"] == "one two three"
        assert row["embeddings"] == [0.1, 0.2]
        assert row["metadatas"]["source_document"] == "known.txt"
        assert row["metadatas"]["classification_level"] == 1
        assert row["metadatas"]["department"] == "Engineering"
        assert row["metadatas"]["visibility"] == "department"


# ---------------------------------------------------------------------------
# Activation failure-safety. Cutover is a pointer flip, not a copy, so the
# previous generation keeps serving through every failure mode below. The
# same guarantees are exercised against the real GenerationStore flow in
# tests/test_generation_cutover.py; these manifest-level tests pin the
# ingestor-side behavior (refuse-before-activate, no half-built state).
# ---------------------------------------------------------------------------



def test_removed_documents_converge_on_reingestion(tmp_path):
    """Removed documents converge: each generation is built fresh from the
    current manifest, so a document dropped from the manifest disappears from
    the newly activated generation."""
    first_manifest = valid_manifest()
    first_manifest["retired.txt"] = {
        "classification_level": 1,
        "department": "Engineering",
        "visibility": "department",
        "content_sha256": hashlib.sha256(b"removed document").hexdigest(),
    }
    ingestor = make_ingestor(tmp_path, first_manifest)
    (tmp_path / "documents" / "retired.txt").write_bytes(b"removed document")
    ingestor.manifest = ingestor._load_manifest()

    class Encoded:
        def tolist(self):
            return [0.1, 0.2]

    ingestor.model = type("Model", (), {"encode": lambda self, text: Encoded()})()
    client = FakeChromaClient()
    ingestor.client = client
    store = SQLiteGenerationStore(
        create_engine(
            "sqlite://",
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
    )
    ingestor.store = store
    ingestor.ingest_directory()

    # Drop retired.txt from the manifest and the corpus, then re-ingest: the
    # new generation must hold only known.txt rows.
    (tmp_path / "documents" / "retired.txt").unlink()
    second_manifest = valid_manifest()
    (tmp_path / "manifest.json").write_text(
        json.dumps(second_manifest), encoding="utf-8"
    )
    ingestor.manifest = ingestor._load_manifest()
    ingestor.ingest_directory()

    store.acquire()
    try:
        active_name = store.active_name()
    finally:
        store.release()
    rows = list(client.collections[active_name].store.values())
    assert rows
    assert {row["metadatas"]["source_document"] for row in rows} == {"known.txt"}
    assert all(row["documents"] == "one two three" for row in rows)


def test_processing_failure_before_activation_keeps_previous_generation(tmp_path):
    """A document-processing crash must leave the previously activated
    generation serving: no pointer flip is attempted after the failure."""
    ingestor = make_ingestor(
        tmp_path,
        {
            "known.txt": {
                "classification_level": 1,
                "department": "Engineering",
                "visibility": "department",
                "content_sha256": KNOWN_SHA256,
            },
            "boom.txt": {
                "classification_level": 1,
                "department": "Engineering",
                "visibility": "department",
                "content_sha256": hashlib.sha256(b"secondary").hexdigest(),
            },
        },
    )
    (tmp_path / "documents" / "boom.txt").write_bytes(b"secondary")
    ingestor.manifest = ingestor._load_manifest()

    class Encoded:
        def tolist(self):
            return [0.1, 0.2]

    ingestor.model = type("Model", (), {"encode": lambda self, text: Encoded()})()
    client = FakeChromaClient()
    ingestor.client = client
    store = SQLiteGenerationStore(
        create_engine(
            "sqlite://",
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
    )
    ingestor.store = store

    # A healthy first run so there is a serving generation to protect.
    ingestor.ingest_directory()
    store.acquire()
    try:
        serving_name = store.active_name()
    finally:
        store.release()
    serving_docs = list(client.collections[serving_name].store.values())

    real_process = ingestor._process_file

    def flaky_process(collection, filename, data):
        if filename == "boom.txt":
            raise RuntimeError("boom-process")
        return real_process(collection, filename, data)

    ingestor._process_file = flaky_process

    with pytest.raises(RuntimeError, match="boom-process"):
        ingestor.ingest_directory()
    # No pointer flip: the previously activated generation still serves,
    # untouched, with its original rows.
    store.acquire()
    try:
        assert store.active_name() == serving_name
    finally:
        store.release()
    assert list(client.collections[serving_name].store.values()) == serving_docs


def test_validation_failure_before_activation_keeps_previous_generation(tmp_path):
    """A generation-validation failure must not flip the pointer: the
    previously activated generation keeps serving."""
    ingestor = make_ingestor(tmp_path, valid_manifest())
    ingestor.manifest = ingestor._load_manifest()

    class Encoded:
        def tolist(self):
            return [0.1, 0.2]

    ingestor.model = type("Model", (), {"encode": lambda self, text: Encoded()})()
    client = FakeChromaClient()
    ingestor.client = client
    store = SQLiteGenerationStore(
        create_engine(
            "sqlite://",
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
    )
    ingestor.store = store

    # A healthy first run so there is a serving generation to protect.
    ingestor.ingest_directory()
    store.acquire()
    try:
        serving_name = store.active_name()
    finally:
        store.release()

    real_validate = ingestor._validate_collection

    def bad_validate(collection, expected_chunks):
        collection.upsert(
            ids=["rogue::chunk-9"],
            embeddings=[[0.0, 0.0]],
            documents=["rogue"],
            metadatas=[
                {
                    "source_document": "unknown.txt",
                    "chunk_index": 9,
                    "classification_level": 1,
                    "department": "Engineering",
                    "visibility": "department",
                }
            ],
        )
        return real_validate(collection, expected_chunks + 1)

    ingestor._validate_collection = bad_validate

    with pytest.raises(ManifestError):
        ingestor.ingest_directory()
    # No pointer flip: the previously activated generation still serves.
    store.acquire()
    try:
        assert store.active_name() == serving_name
    finally:
        store.release()
