import threading
from fastapi import APIRouter, Depends, HTTPException, status, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from gateway.db.session import get_db
from gateway.db.models import User, RequestLog, DenialLog, PiiEvent
from gateway.auth.dependencies import get_current_user
from gateway.middleware.correlation_middleware import get_correlation_id
from gateway.security.pii_detector import redact_pii, detect_pii
from gateway.security.prompt_guard import check_direct_injection, build_messages
from gateway.security.rate_limiter import (
    limiter,
    BURST_LIMIT_STRING,
    SUSTAINED_LIMIT_STRING,
)
from gateway.config import EMBEDDING_MODEL, MAX_PROMPT_CHARS
from gateway.errors import PayloadTooLargeError
from gateway.rag.vector_store import VectorStore
from gateway.llm.ollama_client import OllamaClient

router = APIRouter(prefix="/ask", tags=["query"])

_model = None
_model_lock = threading.Lock()


def get_embedding_model():
    """Load the sentence-transformer lazily.

    `ask` runs inside the threadpool, so concurrent first requests can race here: the lock
    guarantees a single model instance per worker process instead of one per racing thread.
    """
    global _model
    if _model is None:
        with _model_lock:
            if _model is None:
                import sentence_transformers

                # Must match the model used by data/ingest.py at ingestion time, otherwise query
                # vectors would not be comparable with the stored document vectors.
                _model = sentence_transformers.SentenceTransformer(EMBEDDING_MODEL)
    return _model


# Rate limiting (per authenticated user, per endpoint): burst allowance first, then the
# sustained average. slowapi evaluates all limits registered for a route exactly once per
# request, so a request must satisfy both.
@router.post("")
@limiter.limit(SUSTAINED_LIMIT_STRING)
@limiter.limit(BURST_LIMIT_STRING)
def ask(
    request: Request,
    message: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # Server-generated correlation id: a client-supplied X-Request-ID is discarded by the
    # middleware. The same id is returned in the body, sent in the X-Request-ID response
    # header and used for every audit row written for this request.
    request_id = get_correlation_id(request)

    # Reject an oversized prompt before anything else happens: otherwise the full payload would
    # be embedded, written to the audit trail and forwarded to the LLM.
    if len(message) > MAX_PROMPT_CHARS:
        raise PayloadTooLargeError(
            f"Message exceeds the {MAX_PROMPT_CHARS} character limit"
        )

    # Scrub PII first so raw sensitive data is never persisted to denial_logs or request_logs
    redacted_prompt, pii_findings = redact_pii(message)

    for finding in pii_findings:
        pii_event = PiiEvent(
            request_id=request_id,
            user_id=current_user.id,
            pii_type=finding["type"],
            action="REDACTED",
            masked_value=finding["mask"],
        )
        db.add(pii_event)
    if pii_findings:
        # Commit the redaction audit rows before any downstream call so that a later dependency
        # failure (vector store / LLM) cannot roll back the evidence that PII was scrubbed.
        db.commit()

    injection_detected, injection_details = check_direct_injection(redacted_prompt)
    if injection_detected:
        denial = DenialLog(
            request_id=request_id,
            user_id=current_user.id,
            department=current_user.department,
            clearance_level=current_user.clearance_level,
            reason_code="DIRECT_INJECTION",
            reason_detail="Direct prompt injection detected in user message",
            sanitized_prompt=redacted_prompt,
        )
        db.add(denial)
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Request blocked: prompt injection detected",
        )

    vector_store = VectorStore()
    model = get_embedding_model()
    query_embedding = model.encode(redacted_prompt).tolist()
    results = vector_store.query(query_embedding, current_user.clearance_level, n_results=5)

    documents = []
    sources = []
    if results and results.get("documents") and results["documents"]:
        for doc in results["documents"][0]:
            documents.append(doc)
        if results.get("metadatas") and results["metadatas"]:
            for meta in results["metadatas"][0]:
                sources.append(meta.get("source_document", "unknown"))

    if not documents:
        llm_response = "No relevant documents found for your query at your clearance level."
        prompt_tokens = 0
        completion_tokens = 0
    else:
        messages = build_messages(redacted_prompt, documents)
        ollama = OllamaClient()
        result = ollama.chat(messages)
        llm_response = result["response"]
        prompt_tokens = result["prompt_eval_count"]
        completion_tokens = result["eval_count"]

    log_entry = RequestLog(
        request_id=request_id,
        user_id=current_user.id,
        department=current_user.department,
        clearance_level=current_user.clearance_level,
        sanitized_prompt=redacted_prompt,
        llm_response=llm_response,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        status="success",
    )
    db.add(log_entry)
    db.commit()

    return JSONResponse(
        content={
            "response": llm_response,
            "sources": list(set(sources)) if sources else [],
            "request_id": request_id,
        }
    )