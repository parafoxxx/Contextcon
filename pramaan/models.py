"""Data models: what Claude extracts from the resume, and what the checks produce."""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


# ---------- Extracted from the resume (Claude structured output) ----------

class JobClaim(BaseModel):
    company: str = Field(description="Employer name exactly as written on the resume")
    title: str = Field(description="Job title exactly as written")
    start: Optional[str] = Field(None, description="Start date as written, e.g. 'Mar 2019', '2019', '03/2019'")
    end: Optional[str] = Field(None, description="End date as written, or 'Present'")
    employment_type: Optional[str] = Field(None, description="full-time, internship, contract, freelance, part-time, advisor, if stated or obvious")
    via_staffing_firm: Optional[str] = Field(None, description="If the person was deployed via a staffing/IT services firm (e.g. 'TCS, client: Barclays'), the staffing firm name")
    team_size_claim: Optional[int] = Field(None, description="Number of people the candidate says they managed or led in this role, if stated")
    early_employee_claim: Optional[int] = Field(None, description="If they claim to be 'employee #N' or 'one of the first N employees', N")
    joined_stage_claim: Optional[str] = Field(None, description="Company funding stage the candidate claims at the time they joined, normalized to one of: bootstrapped, pre_seed, seed, series_a, series_b, series_c, series_d_plus, public")
    stage_claim_text: Optional[str] = Field(None, description="The exact resume text behind team_size_claim / early_employee_claim / joined_stage_claim")
    bullets: list[str] = Field(default_factory=list, description="Achievement bullets, verbatim, max 6")


class EducationClaim(BaseModel):
    school: str
    degree: Optional[str] = None
    field: Optional[str] = None
    start_year: Optional[int] = None
    end_year: Optional[int] = None


class ReferenceClaim(BaseModel):
    name: str
    company: Optional[str] = None
    relationship: Optional[str] = None
    email: Optional[str] = None


class ResumeClaims(BaseModel):
    name: str
    email: Optional[str] = None
    phone: Optional[str] = None
    linkedin_url: Optional[str] = None
    location: Optional[str] = None
    jobs: list[JobClaim] = Field(default_factory=list)
    education: list[EducationClaim] = Field(default_factory=list)
    references: list[ReferenceClaim] = Field(default_factory=list)
    extraction_notes: list[str] = Field(default_factory=list, description="Anything unreadable, ambiguous or suspicious about the document itself")


# ---------- Verification output ----------

class Status(str, Enum):
    VERIFIED = "verified"          # independent evidence agrees
    CONSISTENT = "consistent"      # only candidate-controlled evidence agrees
    DISCREPANCY = "discrepancy"    # evidence contradicts the claim
    UNVERIFIED = "unverified"      # no evidence either way -> outreach
    REVIEW = "review"              # ambiguous -> human
    PENDING = "pending"            # waiting on an email reply / document


class ControlledBy(str, Enum):
    CANDIDATE = "candidate"        # profile the candidate writes themselves
    COMPANY = "company"            # company records: founded, headcount, funding
    THIRD_PARTY = "third_party"    # colleagues' profiles, web, press, HR replies
    INTERNAL = "internal"          # the resume contradicting itself


class Evidence(BaseModel):
    source: str                    # e.g. "crustdata:/company/enrich"
    record: Optional[str] = None   # e.g. "crustdata_company_id=633593"
    field: Optional[str] = None    # e.g. "basic_info.year_founded"
    value: Optional[str] = None    # what the source says
    controlled_by: ControlledBy
    url: Optional[str] = None
    retrieved_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
    note: Optional[str] = None


class Verdict(BaseModel):
    claim_id: str                  # e.g. "job1.dates"
    claim_text: str
    check: str                     # e.g. "dates", "title", "team_size_feasibility"
    status: Status
    confidence: str = "medium"     # high | medium | low
    reason: str
    evidence: list[Evidence] = Field(default_factory=list)
    next_action: Optional[str] = None


class Report(BaseModel):
    run_id: str
    candidate: str
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
    consent_recorded: bool = False
    identity: dict = Field(default_factory=dict)
    claims: Optional[ResumeClaims] = None
    verdicts: list[Verdict] = Field(default_factory=list)
    outreach: list[dict] = Field(default_factory=list)
    overall: str = "pending"
    summary: str = ""
    duration_s: float = 0.0
    credits_used: float = 0.0
