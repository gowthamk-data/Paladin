from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, status, Request
from sqlalchemy.orm import Session
from gateway.db.session import get_db
from gateway.db.models import User, AuthFailureLog
from gateway.auth.password_utils import verify_password
from gateway.auth.jwt_utils import create_access_token
from gateway.auth.dependencies import get_current_user
from gateway.middleware.correlation_middleware import get_correlation_id
from gateway.config import JWT_EXPIRY_MINUTES

router = APIRouter(prefix="/auth", tags=["authentication"])


@router.post("/login")
def login(
    request: Request,
    username: str,
    password: str,
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.username == username).first()
    if user is None or not verify_password(password, user.password_hash):
        db.add(
            AuthFailureLog(
                request_id=get_correlation_id(request),
                username_attempted=username[:80],
                failure_reason="UNKNOWN_USER" if user is None else "INVALID_PASSWORD",
                ip_address=request.client.host if request.client else None,
            )
        )
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token_data = {
        "sub": str(user.id),
        "username": user.username,
        "department": user.department,
        "clearance_level": user.clearance_level,
        "role": user.role,
    }
    token = create_access_token(token_data, expires_minutes=JWT_EXPIRY_MINUTES)

    return {
        "access_token": token,
        "token_type": "bearer",
        "expires_in": JWT_EXPIRY_MINUTES * 60,
    }


@router.get("/me")
def get_me(current_user: User = Depends(get_current_user)):
    return {
        "user_id": current_user.id,
        "username": current_user.username,
        "department": current_user.department,
        "clearance_level": current_user.clearance_level,
        "role": current_user.role,
        "created_at": current_user.created_at,
    }
