"""Password hashing and request authentication helpers."""
from __future__ import annotations

import hashlib
import hmac
import os

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import User

_ITERATIONS = 390_000


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _ITERATIONS)
    return f"pbkdf2_sha256${_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iterations, salt, digest = stored.split("$")
    except ValueError:
        return False
    if algo != "pbkdf2_sha256":
        return False
    candidate = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(iterations))
    return hmac.compare_digest(candidate.hex(), digest)


class NotAuthenticated(Exception):
    pass


def current_user(request: Request, session: Session = Depends(get_db)) -> User:
    uid = request.session.get("uid")
    user = session.get(User, uid) if uid else None
    if user is None or not user.active:
        raise NotAuthenticated()
    return user


def manager_user(user: User = Depends(current_user)) -> User:
    if not user.is_manager:
        raise HTTPException(status_code=403, detail="Managers only")
    return user


def admin_user(user: User = Depends(current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admins only")
    return user
