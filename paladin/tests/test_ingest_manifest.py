import hashlib
import json

import pytest

from data.ingest import DocumentIngestor, ManifestError

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
    # reach processing, staging validation, and promotion. Fake Chroma
    # collections and a mock embedding model: no live Compose stack.
    ingestor = make_ingestor(tmp_path, valid_manifest())
    ingestor.manifest = ingestor._load_manifest()

    real_process_file = ingestor._process_file

    def mutating_process_file(collection, filename, data):
        # Mutate the source file mid-run, after _files() verified it but
        # before processing consumes the snapshot. Production code never
        # re-reads the file, so tampered bytes must never reach staging.
        (tmp_path / "documents" / filename).write_bytes(b"tampered after verify")
        return real_process_file(collection, filename, data)

    ingestor._process_file = mutating_process_file

    class Encoded:
        def tolist(self):
            return [0.1, 0.2]

    ingestor.model = type("Model", (), {"encode": lambda self, text: Encoded()})()

    class FakeCollection:
        def __init__(self):
            self.rows = []

        def upsert(self, **kwargs):
            ids = kwargs["ids"]
            for index, doc_id in enumerate(ids):
                self.rows.append(
                    {
                        "ids": doc_id,
                        "embeddings": kwargs["embeddings"][index],
                        "documents": kwargs["documents"][index],
                        "metadatas": kwargs["metadatas"][index],
                    }
                )

        def get(self, include=None):
            return {
                "ids": [row["ids"] for row in self.rows],
                "embeddings": [row["embeddings"] for row in self.rows],
                "documents": [row["documents"] for row in self.rows],
                "metadatas": [row["metadatas"] for row in self.rows],
            }

        def delete(self, ids):
            self.rows = [row for row in self.rows if row["ids"] not in ids]

    staging, active = FakeCollection(), FakeCollection()

    class FakeClient:
        def get_or_create_collection(self, **kwargs):
            if kwargs.get("name").endswith("_staging"):
                return staging
            return active

        def create_collection(self, **kwargs):
            if kwargs.get("name").endswith("_staging"):
                return staging
            return active

        def get_collection(self, **kwargs):
            if kwargs.get("name").endswith("_staging"):
                return staging
            raise Exception("active collection does not exist yet")

    ingestor.client = FakeClient()

    real_validate = ingestor._validate_collection
    validate_calls = []

    def validate_spy(collection, expected_chunks):
        validate_calls.append(expected_chunks)
        return real_validate(collection, expected_chunks)

    ingestor._validate_collection = validate_spy

    total = ingestor.ingest_directory()

    # 1. Chunk count: the returned total must equal what staging holds, and
    #    staged validation must have been invoked exactly once with that count.
    assert total > 0, "expected at least one chunk from the fixture document"
    assert total == len(staging.rows), (
        f"returned count {total} != staged rows {len(staging.rows)}"
    )
    assert validate_calls == [total], (
        f"staged validation not invoked with the expected count: {validate_calls}"
    )

    # The mid-run mutation really happened, so the assertions below are
    # meaningful: bytes on disk now differ from the verified snapshot.
    assert (tmp_path / "documents" / "known.txt").read_bytes() == (
        b"tampered after verify"
    )

    # 2. Verified bytes only: tampered bytes never reached staging, while the
    #    verified snapshot text did.
    assert staging.rows, "staging collection received no chunks"
    assert all(
        "tampered" not in row["documents"] for row in staging.rows
    ), "unverified bytes reached staging"
    assert any("one two three" in row["documents"] for row in staging.rows)

    # 3. Deep promotion comparison: every active row must exactly equal its
    #    staging row (ids, documents, embeddings, metadata), so any dropped
    #    or altered row during promotion fails the test.
    assert len(active.rows) == len(staging.rows) == total, (
        "promotion did not carry every verified chunk to active"
    )
    staged_by_id = {row["ids"]: row for row in staging.rows}
    active_by_id = {row["ids"]: row for row in active.rows}
    assert set(active_by_id) == set(staged_by_id), (
        "active chunk IDs differ from staging chunk IDs"
    )
    for doc_id, staged_row in staged_by_id.items():
        active_row = active_by_id[doc_id]
        assert active_row["documents"] == staged_row["documents"], doc_id
        assert active_row["embeddings"] == staged_row["embeddings"] == (
            [0.1, 0.2]
        ), doc_id
        assert active_row["metadatas"] == staged_row["metadatas"], doc_id
    for row in active.rows:
        assert row["metadatas"]["source_document"] == "known.txt"
        assert row["metadatas"]["classification_level"] == 1
        assert row["metadatas"]["department"] == "Engineering"
        assert row["metadatas"]["visibility"] == "department"
