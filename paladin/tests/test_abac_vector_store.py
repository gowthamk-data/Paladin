import pytest

from gateway.authz.policy import UserAttributes
from gateway.errors import AuthorizationDeniedError
from gateway.rag.vector_store import VectorStore


class FakeCollection:
    def __init__(self, results):
        self.results = results
        self.where = None

    def query(self, **kwargs):
        self.where = kwargs["where"]
        return self.results


def test_secondary_check_removes_no_authorized_chunks_and_applies_full_filter():
    store = VectorStore.__new__(VectorStore)
    collection = FakeCollection(
        {
            "documents": [["engineering"]],
            "metadatas": [
                [
                    {
                        "source_document": "engineering_policy.txt",
                        "classification_level": 1,
                        "department": "Engineering",
                        "visibility": "department",
                    }
                ]
            ],
        }
    )
    store.collection = collection
    results = store.query([], UserAttributes("Engineering", 1, "employee"))
    assert results["documents"] == [["engineering"]]
    assert collection.where["$and"][0] == {"classification_level": {"$lte": 1}}


def test_secondary_check_fails_closed_on_filter_bypass():
    store = VectorStore.__new__(VectorStore)
    store.collection = FakeCollection(
        {
            "documents": [["secret"]],
            "metadatas": [
                [
                    {
                        "source_document": "finance_guidelines.txt",
                        "classification_level": 3,
                        "department": "Finance",
                        "visibility": "department",
                    }
                ]
            ],
        }
    )
    with pytest.raises(AuthorizationDeniedError):
        store.query([], UserAttributes("Engineering", 1, "employee"))
