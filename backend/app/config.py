from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """CARA backend settings. Env vars are prefixed CARA_ (e.g. CARA_DATA_DIR)."""

    model_config = SettingsConfigDict(env_prefix="CARA_", env_file=".env", extra="ignore")

    # All persistent state lives under this dir (bind-mounted to ./data on the host).
    data_dir: Path = Path("/data")

    # Admin bootstrap (used only to create the first admin if none exists).
    admin_username: str = "admin"
    admin_password: str = "change-me-now"

    # Session signing + credential encryption.
    secret_key: str = "dev-insecure-change-me"
    fernet_key: str = ""  # blank -> auto-generated and persisted to secret.key

    # RAG.
    embed_model: str = "BAAI/bge-small-en-v1.5"
    chunk_size: int = 1000
    chunk_overlap: int = 150

    # Shown in the admin UI so staff know who to contact for help.
    support_contact: str = ""

    # IANA timezone for "today / this week / overdue" date logic.
    timezone: str = "America/Chicago"

    # Base URL the user's browser uses to reach this backend (for printable PDF links).
    public_base_url: str = "http://localhost:8000"

    # Bearer token required by the /tools OpenAPI server (set the same value in Open WebUI's
    # tool-server auth). Empty = open. Set this to lock the data tools down on a shared network.
    tools_token: str = ""

    @property
    def cara_dir(self) -> Path:
        return self.data_dir / "cara"

    @property
    def db_path(self) -> Path:
        return self.cara_dir / "cara.db"

    @property
    def chroma_dir(self) -> Path:
        return self.data_dir / "chroma"

    @property
    def documents_dir(self) -> Path:
        return self.data_dir / "documents"

    @property
    def fernet_key_path(self) -> Path:
        return self.cara_dir / "secret.key"


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    # Ensure the directory tree exists (idempotent).
    s.cara_dir.mkdir(parents=True, exist_ok=True)
    s.chroma_dir.mkdir(parents=True, exist_ok=True)
    for sub in ("manuals", "workflows", "inventory"):
        (s.documents_dir / sub).mkdir(parents=True, exist_ok=True)
    return s
