from __future__ import annotations

from sqlmodel import Session, SQLModel, create_engine

from .config import get_settings

_settings = get_settings()
_engine = create_engine(
    f"sqlite:///{_settings.db_path}",
    connect_args={"check_same_thread": False},
)


def init_db() -> None:
    # Importing models registers the tables on SQLModel.metadata.
    from . import models  # noqa: F401

    SQLModel.metadata.create_all(_engine)
    _migrate()


def _migrate() -> None:
    """Lightweight additive migrations (SQLite ADD COLUMN) for columns added to a model after
    its table was first created; create_all does not alter existing tables."""
    from sqlalchemy import text

    additions = {"mondayjob": {"hard_date": "VARCHAR"}}
    with _engine.begin() as conn:
        for table, cols in additions.items():
            existing = {r[1] for r in conn.execute(text(f"PRAGMA table_info({table})"))}
            if not existing:
                continue  # fresh DB — create_all already built the table with all columns
            for name, sqltype in cols.items():
                if name not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {sqltype}"))


def get_session() -> Session:
    return Session(_engine)


def get_engine():
    return _engine
