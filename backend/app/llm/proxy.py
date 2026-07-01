"""OpenAI-compatible chat proxy: Open WebUI -> here -> LM Studio.

Open WebUI's OPENAI_API_BASE_URL points at /llm/v1. For each chat request we classify the latest
user message (a quick, no-reasoning model call) as an ORDER question (fast, non-thinking) or a
HOW-TO / machine question (thinking), then apply the admin-configured per-mode sampling params and
the reasoning toggle, and forward to LM Studio (streaming or not). Tool-calling and the reasoning
("think") block are handled by Open WebUI as usual — we relay the upstream response verbatim.

Thinking is toggled via the OpenAI-standard `reasoning_effort` (verified against
qwen/qwen3.6-35b-a3b in LM Studio): "none" disables the think block (quick), a normal effort
enables it (thinking). The `/no_think` token and `enable_thinking` flag are NOT honored by this
runtime, so we do not rely on them.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time

import httpx
from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import Response, StreamingResponse

from ..config import get_settings
from ..kv import get_setting_float, get_setting_int

log = logging.getLogger("cara.llm")
_settings = get_settings()

router = APIRouter(prefix="/llm/v1", tags=["llm-proxy"])

# reasoning_effort per mode. "none" => no think block (quick); "high" => think (thorough).
_EFFORT = {"quick": "none", "thinking": "high"}
_DEFAULT_MODE = "quick"  # ambiguous -> favor speed

# Per-mode sampling defaults (Qwen-recommended); used when the admin hasn't set a value.
_DEFAULTS = {
    "quick":    {"temperature": 0.7, "top_p": 0.80, "top_k": 20, "presence_penalty": 0.0, "repeat_penalty": 1.0},
    "thinking": {"temperature": 0.6, "top_p": 0.95, "top_k": 20, "presence_penalty": 0.0, "repeat_penalty": 1.0},
}

# Tiny TTL cache so the tool-call follow-up rounds of one turn don't re-classify the same question.
_CACHE: dict[str, tuple[str, float]] = {}
_TTL = 300.0

_CLASSIFIER_SYS = (
    "You route a shop assistant's messages. Reply with EXACTLY one word: ORDER or HOWTO.\n"
    "ORDER = anything about customer orders or production jobs: order/estimate/BC/SR numbers, "
    "status, what's due, dates, invoices or payment, customers, or inventory/stock levels.\n"
    "HOWTO = how to operate, set up, fix, calibrate, clean, or do maintenance on a machine or "
    "piece of software (manuals / troubleshooting).\n"
    "If unsure, answer ORDER. Reply with only ORDER or HOWTO."
)


def _require_key(authorization: str | None) -> None:
    key = _settings.llm_proxy_key
    if not key:
        return
    if not (authorization and hmac.compare_digest(authorization.strip(), f"Bearer {key}")):
        raise HTTPException(status_code=401, detail="Unauthorized")


def _last_user_text(messages: list) -> str:
    for m in reversed(messages or []):
        if isinstance(m, dict) and m.get("role") == "user":
            c = m.get("content")
            if isinstance(c, str):
                return c
            if isinstance(c, list):  # multimodal content parts -> join the text parts
                return " ".join(
                    p.get("text", "") for p in c if isinstance(p, dict) and p.get("type") == "text"
                )
    return ""


def _params_for(mode: str) -> dict:
    d = _DEFAULTS[mode]
    return {
        "temperature": get_setting_float(f"llm_temperature_{mode}", d["temperature"]),
        "top_p": get_setting_float(f"llm_top_p_{mode}", d["top_p"]),
        "top_k": get_setting_int(f"llm_top_k_{mode}", d["top_k"]),
        "presence_penalty": get_setting_float(f"llm_presence_penalty_{mode}", d["presence_penalty"]),
        # admin label is "repetition_penalty"; LM Studio's key is repeat_penalty.
        "repeat_penalty": get_setting_float(f"llm_repetition_penalty_{mode}", d["repeat_penalty"]),
    }


async def _classify(client: httpx.AsyncClient, model: str, text: str) -> str:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _CLASSIFIER_SYS},
            {"role": "user", "content": text[:2000]},
        ],
        "reasoning_effort": "none",
        "temperature": 0,
        "max_tokens": 4,
        "stream": False,
    }
    try:
        r = await client.post(
            "/chat/completions", json=payload,
            headers={"Authorization": f"Bearer {_settings.llm_upstream_key}"},
        )
        r.raise_for_status()
        out = (r.json()["choices"][0]["message"].get("content") or "").strip().upper()
        return "thinking" if "HOWTO" in out else "quick"
    except Exception as e:  # noqa: BLE001 — classification must never break chat
        log.warning("llm classifier failed (%s); defaulting to %s", e, _DEFAULT_MODE)
        return _DEFAULT_MODE


async def _decide_mode(client: httpx.AsyncClient, model: str, messages: list) -> str:
    text = _last_user_text(messages)
    if not text:
        return _DEFAULT_MODE
    k = hashlib.sha256(text.encode()).hexdigest()
    now = time.monotonic()
    hit = _CACHE.get(k)
    if hit and hit[1] > now:
        return hit[0]
    mode = await _classify(client, model, text)
    _CACHE[k] = (mode, now + _TTL)
    if len(_CACHE) > 512:  # cheap prune of expired entries
        for kk in [kk for kk, (_, exp) in list(_CACHE.items()) if exp <= now]:
            _CACHE.pop(kk, None)
    return mode


def _apply(body: dict, mode: str) -> dict:
    out = dict(body)
    out.update(_params_for(mode))      # override the 5 sampling params with the per-mode values
    out["reasoning_effort"] = _EFFORT[mode]
    return out


@router.get("/models")
async def models(authorization: str | None = Header(default=None)):
    _require_key(authorization)
    async with httpx.AsyncClient(base_url=_settings.llm_upstream, timeout=30) as client:
        r = await client.get(
            "/models", headers={"Authorization": f"Bearer {_settings.llm_upstream_key}"}
        )
        return Response(
            content=r.content, status_code=r.status_code,
            media_type=r.headers.get("content-type", "application/json"),
        )


@router.post("/chat/completions")
async def chat_completions(request: Request, authorization: str | None = Header(default=None)):
    _require_key(authorization)
    body = await request.json()
    model = body.get("model") or ""
    stream = bool(body.get("stream"))
    headers = {"Authorization": f"Bearer {_settings.llm_upstream_key}"}

    async with httpx.AsyncClient(base_url=_settings.llm_upstream, timeout=30) as cclient:
        mode = await _decide_mode(cclient, model, body.get("messages", []))

    out = _apply(body, mode)
    log.info("llm proxy: mode=%s effort=%s model=%s stream=%s", mode, out["reasoning_effort"], model, stream)

    if stream:
        async def gen():
            timeout = httpx.Timeout(connect=15.0, read=None, write=60.0, pool=None)
            async with httpx.AsyncClient(base_url=_settings.llm_upstream, timeout=timeout) as client:
                async with client.stream("POST", "/chat/completions", json=out, headers=headers) as r:
                    async for chunk in r.aiter_bytes():
                        yield chunk
        return StreamingResponse(gen(), media_type="text/event-stream")

    timeout = httpx.Timeout(connect=15.0, read=600.0, write=60.0, pool=None)
    async with httpx.AsyncClient(base_url=_settings.llm_upstream, timeout=timeout) as client:
        r = await client.post("/chat/completions", json=out, headers=headers)
        return Response(
            content=r.content, status_code=r.status_code,
            media_type=r.headers.get("content-type", "application/json"),
        )
