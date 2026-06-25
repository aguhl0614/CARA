from __future__ import annotations

from functools import lru_cache

from ..config import get_settings

_settings = get_settings()


@lru_cache
def _model():
    # Imported lazily so the app starts even before the model is downloaded.
    from fastembed import TextEmbedding

    return TextEmbedding(model_name=_settings.embed_model)


def embed_texts(texts: list[str]) -> list[list[float]]:
    return [list(map(float, v)) for v in _model().embed(list(texts))]
