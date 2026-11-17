import copy
import unittest

from scripts.read_only_chroma_fingerprint import fingerprint_collection


class FakeCollection:
    def __init__(self, rows):
        self.rows = copy.deepcopy(rows)

    def count(self):
        return len(self.rows["ids"])

    def get(self, include):
        if include != ["documents", "metadatas", "embeddings"]:
            raise AssertionError(f"unexpected include fields: {include!r}")
        return copy.deepcopy(self.rows)


def sample_rows():
    return {
        "ids": ["b", "a"],
        "documents": ["second", "first"],
        "metadatas": [{"department": "Finance", "level": 2}, {"level": 1}],
        "embeddings": [[0.25, -0.5], [1.0, 0.0]],
    }


class ReadOnlyChromaFingerprintTests(unittest.TestCase):
    def test_identical_rows_hash_identically_independent_of_input_order(self):
        rows = sample_rows()
        permuted = {
            key: list(reversed(value)) for key, value in rows.items()
        }
        self.assertEqual(
            fingerprint_collection(FakeCollection(rows)),
            fingerprint_collection(FakeCollection(permuted)),
        )

    def test_document_metadata_and_embedding_changes_change_hash(self):
        baseline_count, baseline_hash = fingerprint_collection(
            FakeCollection(sample_rows())
        )
        self.assertEqual(baseline_count, 2)

        mutations = (
            ("documents", 0, "second!"),
            ("metadatas", 0, {"department": "Engineering", "level": 2}),
            ("embeddings", 0, [0.25, -0.499]),
        )
        for field, index, changed_value in mutations:
            with self.subTest(field=field):
                changed = sample_rows()
                changed[field][index] = changed_value
                self.assertNotEqual(
                    baseline_hash,
                    fingerprint_collection(FakeCollection(changed))[1],
                )


if __name__ == "__main__":
    unittest.main()
