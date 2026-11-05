"""Model calls (Gemini or Claude). The model only reads and interprets documents; it never decides a verdict
on its own. Verdicts come from the rules in pulse.py."""
from __future__ import annotations

import base64
from typing import Optional

import anthropic
from pydantic import BaseModel

from . import config

_client: Optional[anthropic.Anthropic] = None


def client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY or None, max_retries=3)
    return _client


class LLMError(RuntimeError):
    pass


def _doc_block(data: bytes, filename: str) -> dict:
    name = filename.lower()
    b64 = base64.standard_b64encode(data).decode()
    if name.endswith(".pdf"):
        return {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": b64}}
    for ext, mt in ((".png", "image/png"), (".jpg", "image/jpeg"), (".jpeg", "image/jpeg"), (".webp", "image/webp")):
        if name.endswith(ext):
            return {"type": "image", "source": {"type": "base64", "media_type": mt, "data": b64}}
    return {"type": "text", "text": data.decode("utf-8", errors="replace")}


def _mime(filename: str) -> str:
    n = filename.lower()
    for ext, mt in ((".pdf", "application/pdf"), (".png", "image/png"), (".jpg", "image/jpeg"),
                    (".jpeg", "image/jpeg"), (".webp", "image/webp")):
        if n.endswith(ext):
            return mt
    return "text/plain"


def _parse(schema: type[BaseModel], system: str, text: str, doc: tuple[bytes, str] | None = None, effort: str = "low"):
    """One structured-output call, cached on its exact input so the same case always gets the same reading."""
    import hashlib
    import json as _json
    h = hashlib.sha256()
    for part in (config.LLM_PROVIDER, schema.__name__, _json.dumps(schema.model_json_schema(), sort_keys=True), system, text):
        h.update(part.encode("utf-8", "replace"))
    if doc:
        h.update(doc[0])
    cf = config.CACHE_DIR / f"llm_{h.hexdigest()[:24]}.json"
    if cf.exists():
        try:
            return schema.model_validate_json(cf.read_text(encoding="utf-8"))
        except ValueError:
            pass
    out = _parse_uncached(schema, system, text, doc, effort)
    cf.write_text(out.model_dump_json(), encoding="utf-8")
    return out


def _parse_uncached(schema, system, text, doc=None, effort="low"):
    if config.LLM_PROVIDER == "gemini":
        return _parse_gemini(schema, system, text, doc)
    content = ([_doc_block(*doc)] if doc else []) + [{"type": "text", "text": text}]
    try:
        resp = client().messages.parse(
            model=config.MODEL,
            max_tokens=16000,
            system=system,
            messages=[{"role": "user", "content": content}],
            output_format=schema,
            output_config={"effort": effort},
        )
    except anthropic.APIStatusError as e:
        raise LLMError(f"Claude API error {e.status_code}: {e.message}") from e
    except anthropic.APIConnectionError as e:
        raise LLMError(f"Claude connection error: {e}") from e
    if resp.stop_reason == "refusal":
        raise LLMError("Claude declined to process this document")
    if resp.stop_reason == "max_tokens" or resp.parsed_output is None:
        raise LLMError(f"Could not parse structured output (stop_reason={resp.stop_reason})")
    return resp.parsed_output


_gclient = None
_glock = __import__("threading").Lock()


def gemini_client():
    """Keep one client alive: a client that goes out of scope closes its connection mid-request."""
    global _gclient
    with _glock:
        return _gclient or _make_gclient()


def _make_gclient():
    global _gclient
    if _gclient is None:
        from google import genai
        from google.genai import types
        # Our own loop handles retries and model fallback, so: one attempt per call, 60 s cap.
        _gclient = genai.Client(api_key=config.GEMINI_API_KEY, http_options=types.HttpOptions(
            timeout=45_000, retry_options=types.HttpRetryOptions(attempts=1)))
    return _gclient


def _parse_gemini(schema: type[BaseModel], system: str, text: str, doc):
    from google import genai
    from google.genai import errors, types
    if not config.GEMINI_API_KEY:
        raise LLMError("GEMINI_API_KEY not set")
    parts = []
    if doc:
        data, filename = doc
        mt = _mime(filename)
        parts.append(types.Part.from_text(text=data.decode("utf-8", errors="replace")) if mt == "text/plain"
                     else types.Part.from_bytes(data=data, mime_type=mt))
    parts.append(types.Part.from_text(text=text))
    import time
    last = None
    # Busy or retired model -> move on to the next one; bad output -> retry once on the same model.
    chain = [config.GEMINI_MODEL] + [m for m in config.GEMINI_FALLBACKS if m != config.GEMINI_MODEL]
    for model in chain:
        for attempt in range(2):
            try:
                resp = gemini_client().models.generate_content(
                    model=model,
                    contents=[types.Content(role="user", parts=parts)],
                    config=types.GenerateContentConfig(
                        system_instruction=system,
                        response_mime_type="application/json",
                        response_json_schema=schema.model_json_schema(),
                        temperature=0,
                    ),
                )
                return schema.model_validate_json(resp.text or "")
            except errors.APIError as e:
                code = getattr(e, "code", None)
                last = f"Gemini API error {code} on {model}: {str(e)[:200]}"
                if code in (429, 500, 503, 504) and attempt == 0:
                    time.sleep(3)
                    continue
                if code in (404, 429, 500, 503, 504):
                    break  # try the next model
                raise LLMError(last) from e  # 400/401/403: our request or key is wrong; no point switching
            except ValueError as e:  # bad JSON / schema mismatch
                last = f"Gemini output on {model} did not match the schema: {e}"
    raise LLMError(last or "Gemini call failed")
