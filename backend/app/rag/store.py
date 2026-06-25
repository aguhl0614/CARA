from __future__ import annotations

from typing import Optional

from ..config import get_settings
from .embed import embed_texts

_settings = get_settings()
_collection = None


def _get_collection():
    """Lazily open the persistent Chroma collection (kept in the bound volume)."""
    global _collection
    if _collection is None:
        import chromadb

        client = chromadb.PersistentClient(path=str(_settings.chroma_dir))
        _collection = client.get_or_create_collection("documents")
    return _collection


def add_chunks(
    doc_id: int,
    machine_ids: list[int],
    title: str,
    doc_type: str,
    chunks: list[str],
    pages: list[int],
) -> None:
    if not chunks:
        return
    col = _get_collection()
    embeddings = embed_texts(chunks)
    ids = [f"{doc_id}:{i}" for i in range(len(chunks))]
    metadatas = [
        {
            "document_id": doc_id,
            "title": title,
            "doc_type": doc_type,
            "page": pages[i] if i < len(pages) else 0,
            # Chroma metadata values must be scalars; store ids as a comma list.
            "machine_ids": "," + ",".join(str(m) for m in machine_ids) + ",",
        }
        for i in range(len(chunks))
    ]
    col.add(ids=ids, documents=chunks, embeddings=embeddings, metadatas=metadatas)


def delete_document(doc_id: int) -> None:
    _get_collection().delete(where={"document_id": doc_id})


def search_documents(query: str, machine: Optional[str] = None, limit: int = 5) -> dict:
    col = _get_collection()
    q_emb = embed_texts([query])[0]
    machine_id = _resolve_machine_id(machine) if machine else None

    # Over-fetch, then keep only chunks tagged with the requested machine (req #4).
    res = col.query(query_embeddings=[q_emb], n_results=max(limit * 4, limit))
    docs = (res.get("documents") or [[]])[0]
    metas = (res.get("metadatas") or [[]])[0]
    dists = (res.get("distances") or [[]])[0]

    results = []
    for doc, meta, dist in zip(docs, metas, dists):
        if machine_id is not None:
            tagged = str(meta.get("machine_ids", ""))
            if f",{machine_id}," not in tagged:
                continue
        results.append(
            {
                "text": doc,
                "title": meta.get("title"),
                "page": meta.get("page"),
                "doc_type": meta.get("doc_type"),
                "score": (1 - dist) if dist is not None else None,
            }
        )
        if len(results) >= limit:
            break
    return {"query": query, "machine": machine, "results": results}


def _resolve_machine_id(machine: str) -> Optional[int]:
    from sqlmodel import select

    from ..db import get_session
    from ..models import Machine

    needle = machine.strip().lower()
    with get_session() as s:
        for row in s.exec(select(Machine)).all():
            names = [row.name.lower()] + [a.lower() for a in (row.aliases or [])]
            if needle in names:
                return row.id
    return None
