from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func
from sqlalchemy.orm import Session
from gateway.db.session import get_db
from gateway.db.models import User, RequestLog, DenialLog, PiiEvent, AuthFailureLog
from gateway.auth.dependencies import get_current_user

router = APIRouter(prefix="/metrics", tags=["metrics"])


@router.get("/stats")
def get_stats(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )

    total_users = db.query(User).count()
    total_requests = db.query(RequestLog).count()
    total_denials = db.query(DenialLog).count()
    total_pii_events = db.query(PiiEvent).count()

    requests_by_department = (
        db.query(RequestLog.department, func.count(RequestLog.id))
        .group_by(RequestLog.department)
        .all()
    )

    denials_by_reason = (
        db.query(DenialLog.reason_code, func.count(DenialLog.id))
        .group_by(DenialLog.reason_code)
        .all()
    )

    token_totals = (
        db.query(
            func.coalesce(func.sum(RequestLog.prompt_tokens), 0),
            func.coalesce(func.sum(RequestLog.completion_tokens), 0),
        ).one()
    )
    total_prompt_tokens = int(token_totals[0] or 0)
    total_completion_tokens = int(token_totals[1] or 0)

    pii_by_type = (
        db.query(PiiEvent.pii_type, func.count(PiiEvent.id))
        .group_by(PiiEvent.pii_type)
        .all()
    )

    auth_failures_by_reason = (
        db.query(AuthFailureLog.failure_reason, func.count(AuthFailureLog.id))
        .group_by(AuthFailureLog.failure_reason)
        .all()
    )

    return {
        "total_users": total_users,
        "total_requests": total_requests,
        "total_denials": total_denials,
        "total_pii_events": total_pii_events,
        "requests_by_department": [
            {"department": dept, "count": count}
            for dept, count in requests_by_department
        ],
        "denials_by_reason": [
            {"reason": reason, "count": count}
            for reason, count in denials_by_reason
        ],
        "total_prompt_tokens": total_prompt_tokens,
        "total_completion_tokens": total_completion_tokens,
        "total_tokens": total_prompt_tokens + total_completion_tokens,
        "pii_events_by_type": [
            {"pii_type": pii_type, "count": count} for pii_type, count in pii_by_type
        ],
        "auth_failures_by_reason": [
            {"failure_reason": reason, "count": count}
            for reason, count in auth_failures_by_reason
        ],
    }
