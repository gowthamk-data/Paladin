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
