from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import JSON, Column, UniqueConstraint
from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AdminUser(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    username: str = Field(index=True, unique=True)
    password_hash: str
    created_at: datetime = Field(default_factory=utcnow)


class Credential(SQLModel, table=True):
    """One row per integration; secrets are Fernet-encrypted in `data_encrypted`."""

    id: Optional[int] = Field(default=None, primary_key=True)
    provider: str = Field(index=True, unique=True)  # quickbooks | monday | bigcommerce
    enabled: bool = False
    data_encrypted: Optional[bytes] = None
    updated_at: datetime = Field(default_factory=utcnow)


class Setting(SQLModel, table=True):
    key: str = Field(primary_key=True)
    value: str = ""


class Machine(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    kind: str = "machine"  # machine | software
    aliases: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    notes: str = ""
    created_at: datetime = Field(default_factory=utcnow)


class Document(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    title: str
    path: str  # relative to settings.documents_dir
    doc_type: str = "manual"  # manual | workflow | inventory
    machine_ids: list[int] = Field(default_factory=list, sa_column=Column(JSON))
    content_hash: Optional[str] = None
    ingested_at: Optional[datetime] = None
    chunk_count: int = 0
    created_at: datetime = Field(default_factory=utcnow)


class Order(SQLModel, table=True):
    """Unified cache of orders from QBO + BigCommerce, enriched with Monday status."""

    __table_args__ = (UniqueConstraint("source", "external_id", name="uq_order_source_extid"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    source: str = Field(index=True)        # quickbooks | bigcommerce
    external_id: str = Field(index=True)   # id within the source system
    number: Optional[str] = Field(default=None, index=True)  # human order/invoice number
    customer: Optional[str] = Field(default=None, index=True)
    status: Optional[str] = None           # status reported by the source
    monday_status: Optional[str] = None    # pipeline status from Monday.com
    monday_item_id: Optional[str] = Field(default=None, index=True)
    total: Optional[float] = None
    currency: Optional[str] = None
    order_date: Optional[datetime] = None
    source_updated_at: Optional[datetime] = None
    raw: dict = Field(default_factory=dict, sa_column=Column(JSON))
    updated_at: datetime = Field(default_factory=utcnow)


class InventoryItem(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    sku: Optional[str] = Field(default=None, index=True)
    name: str = Field(index=True)
    quantity: Optional[float] = None
    unit: Optional[str] = None
    location: Optional[str] = None
    raw: dict = Field(default_factory=dict, sa_column=Column(JSON))
    updated_at: datetime = Field(default_factory=utcnow)


class SyncState(SQLModel, table=True):
    source: str = Field(primary_key=True)  # quickbooks | monday | bigcommerce | inventory
    cursor: Optional[str] = None           # last updated-timestamp or file hash
    last_run_at: Optional[datetime] = None
    last_status: Optional[str] = None      # ok | error
    last_error: Optional[str] = None
    record_count: int = 0
    interval_seconds: int = 600
    enabled: bool = True


class MondayJob(SQLModel, table=True):
    """A row from the Monday 'Jobs' board (production tracker).

    Identifiers are parsed from the item name: leading number = QuickBooks estimate #,
    'BC####' = BigCommerce order #, 'SR#####' = QBO sales receipt #. All status columns
    are captured in `statuses` so any stage (Art, Proof, Production, lasers, …) can be asked about.
    """

    monday_item_id: str = Field(primary_key=True)
    job_number: Optional[str] = Field(default=None, index=True)  # leading # (QBO estimate)
    bc_number: Optional[str] = Field(default=None, index=True)   # BigCommerce order #
    sr_number: Optional[str] = Field(default=None, index=True)   # QBO sales receipt #
    title: str = ""
    main_status: Optional[str] = None
    statuses: dict = Field(default_factory=dict, sa_column=Column(JSON))
    amount: Optional[float] = None
    due_date: Optional[str] = None    # Monday "Due Date" — should be done by
    hard_date: Optional[str] = None   # Monday "Hard Date" — must be done by
    person: Optional[str] = None
    quick_info: Optional[str] = None
    source_updated_at: Optional[datetime] = None
    updated_at: datetime = Field(default_factory=utcnow)
