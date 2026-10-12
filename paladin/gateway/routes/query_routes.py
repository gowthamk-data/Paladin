import logging
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
from gateway.authz.policy import build_chroma_filter
from gateway.errors import (
    AuthorizationDeniedError,
    PayloadTooLargeError,
    ServiceUnavailableError,
)
from gateway.rag.vector_store import VectorStore
from gateway.llm.ollama_client import OllamaClient

router = APIRouter(prefix="/ask", tags=["query"])

logger = logging.getLogger("paladin.query")

# Status recorded on the parent audit row when PII redaction rows were already
# committed but a downstream dependency (vector store / LLM / DB commit) failed
# before the normal `success` row could be written. The value is deliberately
# distinct from `success` so token-aggregation queries are unaffected.
LLM_ERROR_STATUS = "llm_error"

# Fixed, safe payload stored in `llm_response` for the error row. Only the
# already-sanitized prompt and this constant are persisted — never raw PII and
# never exception reprs / driver messages (see gateway/errors.py rule 1).
LLM_ERROR_RESPONSE = "Request could not be completed: downstream dependency unavailable"


def _write_llm_error_log(db: Session, request_id, user: User, redacted_prompt: str) -> bool:
    """Write the parent `request_logs` row for a failed request.

    Returns True when a row was inserted, False when a row for this
    ``request_id`` already exists (idempotency guard against duplicates) or
    when the write itself failed (DB down — the original error is then
    re-raised by the caller and the 503 handler still responds).
    """
    try:
        existing = (
            db.query(RequestLog).filter(RequestLog.request_id == request_id).first()
        )
        if existing is not None:
            return False
        db.add(
            RequestLog(
                request_id=request_id,
                user_id=user.id,
                department=user.department,
                clearance_level=user.clearance_level,
                sanitized_prompt=redacted_prompt,
                llm_response=LLM_ERROR_RESPONSE,
                prompt_tokens=0,
                completion_tokens=0,
                status=LLM_ERROR_STATUS,
            )
        )
        db.commit()
        return True
    except Exception:
        db.rollback()
        logger.error("request_id=%s could not write llm_error audit row", request_id, exc_info=True)
        return False

_model = None
_model_lock = threading.Lock()


def get_embedding_model():
    """Load the sentence-transformer lazily.

    `ask` runs inside the threadpool, so concurrent first requests can race here: the lock
    guarantees a single model instance per worker process instead of one per racing thread.

    A load failure (missing package, unreadable/corrupt model weights, no space left) is a
    dependency outage like any other, so it is converted into a typed
    `ServiceUnavailableError`. Without this such a failure would escape the route as an
    untyped exception: the client would get a 500 with a server-side traceback instead of a
    503, and — because the PII rows are already committed by then — the request would be
    left without its parent `request_logs` row, which is exactly the orphan the audit check
    forbids. The original exception is chained with `from` for server-side logs only and is
    never surfaced to the client.
    """
    global _model
    if _model is None:
        with _model_lock:
            if _model is None:
                try:
                    import sentence_transformers

                    # Must match the model used by data/ingest.py at ingestion time, otherwise query
                    # vectors would not be comparable with the stored document vectors.
                    _model = sentence_transformers.SentenceTransformer(EMBEDDING_MODEL)
                except ServiceUnavailableError:
                    raise
                except Exception as exc:
                    logger.error(
                        "embedding model %s failed to load", EMBEDDING_MODEL, exc_info=True
                    )
                    raise ServiceUnavailableError("embedding model") from exc
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

    try:
        # Constructing the vector store and loading the embedding model are dependency
        # initializations, and both can fail: `VectorStore()` raises a typed
        # ServiceUnavailableError when ChromaDB is unreachable, and `get_embedding_model()`
        # converts a model-load failure into one. They therefore belong *inside* this guard —
        # previously they ran before it, so a failure at either step skipped the
        # `_write_llm_error_log` call below and left the already-committed PII rows without a
        # parent `request_logs` row (the orphan the audit check forbids), and surfaced as a 500
        # rather than a 503.
        # Validate the complete ABAC predicate before touching the model. This is deliberately
        # separate from the vector-store implementation so malformed user attributes fail closed.
        try:
            build_chroma_filter(current_user)
        except ValueError as exc:
            reason_code = str(exc)
            db.add(
                DenialLog(
                    request_id=request_id,
                    user_id=current_user.id,
                    department=None,
                    clearance_level=None,
                    reason_code=reason_code,
                    reason_detail="Authorization attributes are invalid",
                    sanitized_prompt=redacted_prompt,
                )
            )
            db.commit()
            raise HTTPException(status_code=403, detail="Access denied") from exc
        vector_store = VectorStore()
        model = get_embedding_model()
        query_embedding = model.encode(redacted_prompt).tolist()
        results = vector_store.query(query_embedding, current_user, n_results=5)

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
    except AuthorizationDeniedError as exc:
        db.add(
            DenialLog(
                request_id=request_id,
                user_id=current_user.id,
                department=current_user.department,
                clearance_level=current_user.clearance_level,
                reason_code=exc.reason_code,
                reason_detail="Retrieved document failed authorization",
                sanitized_prompt=redacted_prompt,
            )
        )
        db.commit()
        raise HTTPException(status_code=403, detail="Access denied") from exc
    except ServiceUnavailableError:
        # PII redaction rows may already be committed (see above) while no
        # parent `request_logs` row exists yet — that is exactly the orphan the
        # audit check forbids. Write the `llm_error` parent row (sanitized
        # prompt + fixed safe message only; the helper is idempotent), then
        # re-raise so the 503 handler still responds with the typed error.
        _write_llm_error_log(db, request_id, current_user, redacted_prompt)
        raise

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