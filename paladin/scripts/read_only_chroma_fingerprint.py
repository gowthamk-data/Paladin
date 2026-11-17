"""Print count and deterministic content fingerprints for selected Chroma collections.

This utility is read-only: it calls only list/get/count operations. It never
creates, updates, or deletes collections. Run it from an environment that can
reach the intended Chroma server, and do not include document text in reports.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import struct
from typing import Any


DEFAULT_COLLECTIONS = (
    "paladin_docs_abac",
    "paladin_docs_abac_staging",
    "paladin_docs",
)
_DOMAIN = b"PALADIN-CHROMA-FINGERPRINT-v1\0"


def _put_field(hasher: Any, value: bytes) -> None:
    """Length-prefix a field so concatenated values cannot be ambiguous."""
    hasher.update(len(value).to_bytes(8, byteorder="little", signed=False))
    hasher.update(value)


def fingerprint_collection(collection: Any) -> tuple[int, str]:
    """Return record count and SHA-256 over stable, security-relevant row fields."""
    count = collection.count()
    rows = collection.get(include=["documents", "metadatas", "embeddings"])
    ids = rows.get("ids")
    documents = rows.get("documents")
    metadatas = rows.get("metadatas")
    embeddings = rows.get("embeddings")
    if any(value is None for value in (ids, documents, metadatas, embeddings)):
        raise ValueError("collection returned an incomplete fingerprint payload")
    if not (len(ids) == len(documents) == len(metadatas) == len(embeddings) == count):
        raise ValueError("collection count and fingerprint payload lengths differ")
    if len(set(ids)) != len(ids):
        raise ValueError("collection returned duplicate record IDs")

    ordered = sorted(zip(ids, documents, metadatas, embeddings), key=lambda row: row[0])
    digest = hashlib.sha256()
    digest.update(_DOMAIN)
    digest.update(len(ordered).to_bytes(8, byteorder="little", signed=False))

    for record_id, document, metadata, embedding in ordered:
        if not isinstance(record_id, str) or not isinstance(document, str):
            raise ValueError("record ID and document must be strings")
        if not isinstance(metadata, dict):
            raise ValueError("record metadata must be an object")
        if embedding is None or len(embedding) == 0:
            raise ValueError("record embedding must be a non-empty vector")

        id_bytes = record_id.encode("utf-8")
        document_sha256 = hashlib.sha256(document.encode("utf-8")).digest()
        metadata_json = json.dumps(
            metadata,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        embedding_values = tuple(float(value) for value in embedding)
        embedding_bytes = struct.pack(f"<{len(embedding_values)}f", *embedding_values)

        for field in (id_bytes, document_sha256, metadata_json, embedding_bytes):
            _put_field(digest, field)

    return count, digest.hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--collection", action="append", dest="collections")
    args = parser.parse_args(argv)

    import chromadb

    client = chromadb.HttpClient(host=args.host, port=args.port)
    for name in args.collections or DEFAULT_COLLECTIONS:
        collection = client.get_collection(name=name)
        count, fingerprint = fingerprint_collection(collection)
        print(f"{name}|count={count}|sha256={fingerprint}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
