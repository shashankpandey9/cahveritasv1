"""CAH VERITAS — LLM client.

Latency posture:
  * Model tiering — call `fast()` for extraction/classification, `smart()` for
    synthesis. Both async.
  * Prompt caching — pass `cached_context=` and it is sent as a system block
    with `cache_control: ephemeral`; stages 2/3/4 reuse the cached corpus
    digest instead of re-paying the tokens.
  * Bounded concurrency — a process-wide semaphore (VERITAS_CONCURRENCY) so
    per-requirement / per-case fan-outs saturate but never stampede.
  * Small budgets — callers pass max_tokens matched to the shape of the
    answer; the default is deliberately modest.
  * Heuristic degradation — with no ANTHROPIC_API_KEY every call returns
    None and callers fall back to deterministic logic, keeping the full flow
    demo-able offline.

Databricks Model Serving (VERITAS_LLM_PROVIDER=databricks): the Claude models
are called through serving endpoints named by VERITAS_MODEL_FAST / _SMART /
_VISION, with the OpenAI-compatible chat payload the endpoints accept (images
as data-URL image parts), authenticated with the app's Databricks
credentials. Prompt-cache markers are Anthropic-API only and are not sent.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from typing import Any, Dict, List, Optional

import httpx

from . import config

log = logging.getLogger("veritas.llm")

_ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
_RETRYABLE = (429, 500, 529)
_SEM = asyncio.Semaphore(config.CONCURRENCY)
_client: Optional[httpx.AsyncClient] = None


def _serving() -> bool:
    return config.LLM_PROVIDER == "databricks"


def available() -> bool:
    if _serving():
        return bool(config.MODEL_FAST and config.MODEL_SMART)
    return bool(config.ANTHROPIC_API_KEY)


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(90.0, connect=10.0))
    return _client


async def _anthropic(model: str, system_blocks: List[Dict[str, Any]],
                     messages: List[Dict[str, Any]], max_tokens: int,
                     timeout: Optional[float] = None) -> Optional[str]:
    headers = {
        "x-api-key": config.ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload: Dict[str, Any] = {"model": model, "max_tokens": max_tokens,
                               "system": system_blocks, "messages": messages}
    if model.startswith(("claude-opus-5", "claude-fable-5")):
        # These models' safety classifiers can decline a request; let the API
        # re-run a declined request on its recommended fallback model.
        headers["anthropic-beta"] = "server-side-fallback-2026-07-01"
        payload["fallbacks"] = "default"
    for attempt in range(3):
        try:
            r = await _http().post(_ANTHROPIC_URL, headers=headers, json=payload,
                                   timeout=timeout or httpx.USE_CLIENT_DEFAULT)
            if r.status_code in _RETRYABLE:
                await asyncio.sleep(1.2 * (attempt + 1))
                continue
            r.raise_for_status()
            data = r.json()
            if data.get("stop_reason") == "refusal":
                log.warning("model %s declined the request (%s)", model,
                            (data.get("stop_details") or {}).get("category"))
                return None
            return "".join(b.get("text", "") for b in data.get("content", [])
                           if b.get("type") == "text")
        except httpx.HTTPError as e:  # pragma: no cover
            log.warning("anthropic call failed (attempt %s): %s", attempt + 1, e)
            await asyncio.sleep(1.0 * (attempt + 1))
    return None


def _message_text(msg: Dict[str, Any]) -> str:
    """Text of a chat-completions message: content is a string, or a list of
    parts (text / reasoning) for some models."""
    c = msg.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "".join(part.get("text", "") for part in c
                       if isinstance(part, dict) and part.get("type") == "text")
    return ""


async def _dbx_serving(endpoint: str, system_text: str, user_content: Any,
                       max_tokens: int, timeout: Optional[float] = None) -> Optional[str]:
    """One chat completion on a Databricks Model Serving endpoint."""
    from . import dbx
    if not endpoint:
        return None
    url = f"{dbx.host().rstrip('/')}/serving-endpoints/{endpoint}/invocations"
    messages: List[Dict[str, Any]] = []
    if system_text:
        messages.append({"role": "system", "content": system_text})
    messages.append({"role": "user", "content": user_content})
    payload = {"messages": messages, "max_tokens": max_tokens}
    for attempt in range(3):
        try:
            # token refresh may hit the network: keep it off the event loop
            headers = {**await asyncio.to_thread(dbx.auth_headers),
                       "Content-Type": "application/json"}
            r = await _http().post(url, headers=headers, json=payload,
                                   timeout=timeout or httpx.USE_CLIENT_DEFAULT)
            if r.status_code in _RETRYABLE or r.status_code == 503:
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            if r.status_code >= 400:
                log.warning("serving endpoint %s returned %s: %s", endpoint, r.status_code,
                            r.text[:300])
                return None
            choice = (r.json().get("choices") or [{}])[0]
            if choice.get("finish_reason") == "content_filter":
                log.warning("serving endpoint %s declined the request", endpoint)
                return None
            return _message_text(choice.get("message") or {})
        except httpx.HTTPError as e:  # pragma: no cover
            log.warning("serving endpoint %s call failed (attempt %s): %s",
                        endpoint, attempt + 1, e)
            await asyncio.sleep(1.0 * (attempt + 1))
    return None


async def complete(prompt: str, *, model: str, system: str = "",
                   cached_context: str = "", max_tokens: int = 900,
                   timeout: Optional[float] = None) -> Optional[str]:
    """One completion. Returns None when no LLM is configured/reachable —
    callers must have a deterministic fallback."""
    if not available():
        return None
    async with _SEM:
        if _serving():
            sys_text = (system + "\n\n" + cached_context).strip()
            return await _dbx_serving(model, sys_text, prompt, max_tokens, timeout)
        system_blocks: List[Dict[str, Any]] = []
        if system:
            system_blocks.append({"type": "text", "text": system})
        if cached_context:
            block: Dict[str, Any] = {"type": "text", "text": cached_context}
            if config.PROMPT_CACHE:
                block["cache_control"] = {"type": "ephemeral"}
            system_blocks.append(block)
        return await _anthropic(model, system_blocks,
                                [{"role": "user", "content": prompt}], max_tokens,
                                timeout=timeout)


def vision_available() -> bool:
    """Figures can be sent: through the serving endpoints (the vision and fast
    endpoints must serve an image-capable Claude model) or the Anthropic API."""
    if not config.VISION_ENABLED:
        return False
    if _serving():
        return bool(config.MODEL_VISION and config.MODEL_FAST)
    return bool(config.ANTHROPIC_API_KEY)


async def vision(prompt: str, image: bytes, media_type: str, *, model: str,
                 system: str = "", max_tokens: int = 4000,
                 timeout: float = 300.0) -> Optional[str]:
    """One completion over one image (base64 image block before the text).
    Returns None when vision is unavailable, the call fails, or the model
    declines — callers leave the figure pending."""
    if not vision_available():
        return None
    b64 = base64.b64encode(image).decode("ascii")
    if _serving():
        parts = [{"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{b64}"}},
                 {"type": "text", "text": prompt}]
        async with _SEM:
            return await _dbx_serving(model, system, parts, max_tokens, timeout)
    content = [{"type": "image", "source": {"type": "base64", "media_type": media_type,
                                            "data": b64}},
               {"type": "text", "text": prompt}]
    system_blocks = [{"type": "text", "text": system}] if system else []
    async with _SEM:
        return await _anthropic(model, system_blocks, [{"role": "user", "content": content}],
                                max_tokens, timeout=timeout)


async def fast(prompt: str, **kw: Any) -> Optional[str]:
    kw.setdefault("max_tokens", 700)
    return await complete(prompt, model=config.MODEL_FAST, **kw)


async def smart(prompt: str, **kw: Any) -> Optional[str]:
    kw.setdefault("max_tokens", 1600)
    return await complete(prompt, model=config.MODEL_SMART, **kw)


# ---------------------------------------------------------------- json utils
_JSON_RE = re.compile(r"\{.*\}|\[.*\]", re.DOTALL)


def parse_json(text: Optional[str]) -> Optional[Any]:
    """Tolerant JSON extraction: strips prose and ``` fences around a JSON body."""
    if not text:
        return None
    cleaned = text.replace("```json", "```").strip()
    if "```" in cleaned:
        parts = [p for p in cleaned.split("```") if p.strip().startswith(("{", "["))]
        if parts:
            cleaned = parts[0]
    m = _JSON_RE.search(cleaned)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        try:  # trailing commas — common LLM emission
            return json.loads(re.sub(r",\s*([}\]])", r"\1", m.group(0)))
        except json.JSONDecodeError:
            return None


def salvage_objects(text: Optional[str], require_key: str = "") -> List[Dict[str, Any]]:
    """Every complete JSON object in `text`, even when the document is truncated.

    A batched extraction that overruns its token budget produces a valid
    prefix and a severed tail. Discarding the whole response loses the eight
    objects that *did* complete along with the ninth that did not — which in
    a requirements register shows up as requirements silently reverting to
    fallback text. This scans for balanced top-level objects and keeps them.

    `require_key` filters to objects carrying that key, so nested objects and
    the enclosing wrapper are ignored.
    """
    if not text:
        return []
    out: List[Dict[str, Any]] = []
    depth = 0
    start = -1
    in_str = False
    esc = False
    for i, ch in enumerate(text):
        if esc:
            esc = False
            continue
        if ch == "\\" and in_str:
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    try:
                        obj = json.loads(text[start:i + 1])
                    except json.JSONDecodeError:
                        obj = None
                    if isinstance(obj, dict):
                        if not require_key or require_key in obj:
                            out.append(obj)
                        else:  # wrapper — take its listed members instead
                            for v in obj.values():
                                if isinstance(v, list):
                                    out.extend(x for x in v if isinstance(x, dict)
                                               and require_key in x)
                    start = -1
    return out


async def gather_bounded(coros: List[Any]) -> List[Any]:
    """gather() — the semaphore inside complete() enforces the bound."""
    return await asyncio.gather(*coros, return_exceptions=True)
