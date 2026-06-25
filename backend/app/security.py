from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timezone

import bcrypt
from cryptography.fernet import Fernet
from sqlmodel import select

from .config import get_settings
from .db import get_session
from .models import AdminUser, Credential

_settings = get_settings()


# --- password hashing ------------------------------------------------------
def hash_password(password: str) -> str:
    # bcrypt operates on <=72 bytes; truncate defensively.
    return bcrypt.hashpw(password.encode()[:72], bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode()[:72], password_hash.encode())
    except ValueError:
        return False


# --- credential encryption (Fernet) ---------------------------------------
def _load_fernet() -> Fernet:
    key = (_settings.fernet_key or "").strip()
    if not key:
        path = _settings.fernet_key_path
        if path.exists():
            key = path.read_text().strip()
        else:
            key = Fernet.generate_key().decode()
            path.write_text(key)
    return Fernet(key.encode())


_fernet = _load_fernet()


# --- admin bootstrap + auth ------------------------------------------------
def ensure_admin() -> None:
    with get_session() as s:
        if s.exec(select(AdminUser)).first():
            return
        s.add(
            AdminUser(
                username=_settings.admin_username,
                password_hash=hash_password(_settings.admin_password),
            )
        )
        s.commit()


def authenticate(username: str, password: str) -> bool:
    with get_session() as s:
        user = s.exec(select(AdminUser).where(AdminUser.username == username)).first()
        return bool(user and verify_password(password, user.password_hash))


# --- per-provider secret storage -------------------------------------------
def save_credentials(provider: str, data: dict) -> None:
    token = _fernet.encrypt(json.dumps(data).encode())
    with get_session() as s:
        cred = s.exec(select(Credential).where(Credential.provider == provider)).first()
        if not cred:
            cred = Credential(provider=provider)
        cred.data_encrypted = token
        cred.enabled = True
        cred.updated_at = datetime.now(timezone.utc)
        s.add(cred)
        s.commit()


def load_credentials(provider: str) -> dict | None:
    with get_session() as s:
        cred = s.exec(select(Credential).where(Credential.provider == provider)).first()
        if not cred or not cred.data_encrypted:
            return None
        return json.loads(_fernet.decrypt(cred.data_encrypted).decode())


def credential_status() -> dict[str, bool]:
    with get_session() as s:
        rows = s.exec(select(Credential)).all()
        return {c.provider: bool(c.enabled and c.data_encrypted) for c in rows}


def print_token(number: str) -> str:
    """Stateless signed token for a printable-order link (so order PDFs aren't openly fetchable)."""
    return hmac.new(_settings.secret_key.encode(), f"order:{number}".encode(), hashlib.sha256).hexdigest()[:32]


def verify_print_token(number: str, token: str) -> bool:
    return bool(token) and hmac.compare_digest(token, print_token(number))
