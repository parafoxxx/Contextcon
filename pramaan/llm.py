"""Model calls (Claude by default, Gemini optional). The model only reads and interprets documents; it never decides a verdict
on its own. Verdicts come from the deterministic checks in checks.py."""
from __future__ import annotations

import base64
from typing import Optional

import anthropic
from pydantic import BaseModel, Field

from . import config
from .models import ResumeClaims

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
    """One structured-output call. Provider is chosen by LLM_PROVIDER (anthropic | gemini)."""
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
    last = None
    for attempt in range(3):
        try:
            resp = genai.Client(api_key=config.GEMINI_API_KEY).models.generate_content(
                model=config.GEMINI_MODEL,
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
            last = f"Gemini API error {getattr(e, 'code', '')}: {e}"
            if getattr(e, "code", None) in (429, 500, 503):
                import time
                time.sleep(5 * (attempt + 1))
                continue
            break
        except ValueError as e:  # bad JSON / schema mismatch
            last = f"Gemini returned output that did not match the schema: {e}"
    raise LLMError(last or "Gemini call failed")


EXTRACT_SYSTEM = """You extract verifiable claims from a candidate's resume for a background check.
Rules:
- Copy company names, titles and dates exactly as written. Do not correct, guess or fill gaps.
- One JobClaim per role. If one company has several titles over time, make one JobClaim per title.
- Only set team_size_claim / early_employee_claim / joined_stage_claim when the resume explicitly says so.
  "Managed a team of 12" -> 12. "Led 3 squads of ~8" -> 24. "Joined pre-Series A" -> seed.
  "Employee #4" or "one of the first 4 hires" -> early_employee_claim 4.
- If the document is a scan, two-column, or has odd formatting, still extract everything you can read,
  and mention problems in extraction_notes.
- If something in the document itself looks wrong (overlapping dates, dates in the future, a company
  name that looks misspelled), note it in extraction_notes. Do not judge truthfulness otherwise."""


def extract_claims(data: bytes, filename: str) -> ResumeClaims:
    return _parse(ResumeClaims, EXTRACT_SYSTEM, "Extract every verifiable claim from this resume.", (data, filename))


# ---------- Reading an email reply from HR / a reference ----------

class ReplyReading(BaseModel):
    confirms_employment: Optional[bool] = Field(None, description="True if the reply confirms the person worked there, False if it denies, null if unclear")
    confirmed_title: Optional[str] = None
    confirmed_start: Optional[str] = None
    confirmed_end: Optional[str] = None
    confirmed_team_size: Optional[int] = None
    contradicts_claim: bool = Field(description="True if anything in the reply contradicts the specific claim asked about")
    is_clear: bool = Field(description="False if the reply is vague, hedged, conditional, or asks for something first (e.g. consent form)")
    quote: str = Field(description="The exact sentence(s) from the reply that support your reading")
    summary: str


REPLY_SYSTEM = """You read a reply to an employment verification email. Report only what the reply says.
Quote the exact supporting text. If the sender hedges ("I think", "around"), is unsure, or asks for
a consent form or more information before answering, set is_clear=false."""


def read_reply(claim_text: str, question: str, reply: str) -> ReplyReading:
    return _parse(ReplyReading, REPLY_SYSTEM,
                  f"Claim being verified: {claim_text}\nQuestion we asked: {question}\n\nReply received:\n<reply>\n{reply}\n</reply>")


# ---------- Reading a document the candidate uploads to explain a discrepancy ----------

class DocReading(BaseModel):
    document_type: str = Field(description="offer letter, relieving letter, payslip, experience certificate, other")
    issuer: Optional[str] = None
    person_named: Optional[str] = None
    title: Optional[str] = None
    start: Optional[str] = None
    end: Optional[str] = None
    supports_claim: Optional[bool] = Field(None, description="True if it supports the claim, False if it contradicts, null if irrelevant")
    tamper_signals: list[str] = Field(default_factory=list, description="Visual or textual signs of editing, mismatched fonts, impossible dates, missing letterhead")
    quote: str
    summary: str


DOC_SYSTEM = """You inspect a document a job candidate uploaded to support a claim in a background check.
Report what the document says, quote the relevant text, and list any signs it may have been edited.
You do not make the final decision; a human reviews flagged documents."""


def read_document(claim_text: str, data: bytes, filename: str) -> DocReading:
    return _parse(DocReading, DOC_SYSTEM, f"Claim this document is meant to support: {claim_text}", (data, filename), effort="medium")
