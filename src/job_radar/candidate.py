"""Serializable job-search preferences, independent of historical personal filters."""
from __future__ import annotations

import math
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def clean_terms(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        value = value.strip()
        if not value or len(value) > 100 or any(ord(c) < 32 for c in value):
            raise ValueError("词条必须为 1–100 个可打印字符")
        if value.casefold() not in seen:
            result.append(value)
            seen.add(value.casefold())
    return result


class CandidateProfile(BaseModel):
    """An empty city/role list means unrestricted; scores are not hiring probabilities."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)
    cities: list[str] = Field(default_factory=list, max_length=200)
    preferred_cities: list[str] = Field(default_factory=list, max_length=20)
    graduation_year: int | None = Field(default=None, ge=1990, le=2100)
    education: str = Field(default="不限", max_length=30)
    max_experience_years: int = Field(default=40, ge=0, le=40)
    salary_min_k: float = Field(default=0, ge=0, le=300)
    salary_max_k: float = Field(default=300, gt=0, le=300)
    roles: list[str] = Field(default_factory=list, max_length=60)
    skills: list[str] = Field(default_factory=list, max_length=100)
    exclude_internships: bool = False
    exclude_staffing: bool = False
    excluded_companies: list[str] = Field(default_factory=list, max_length=100)
    excluded_titles: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("cities", "preferred_cities", "roles", "skills", "excluded_companies", "excluded_titles")
    @classmethod
    def validate_terms(cls, values: list[str]) -> list[str]:
        return clean_terms(values)

    @model_validator(mode="after")
    def salary_order(self):
        if not all(math.isfinite(n) for n in (self.salary_min_k, self.salary_max_k)):
            raise ValueError("薪资必须是有限数字")
        if self.salary_min_k > self.salary_max_k:
            raise ValueError("期望薪资下限不能高于上限")
        return self


def passes_list_facts(row: dict, profile: CandidateProfile) -> bool:
    """Use editable factual preferences before fetching details; missing JD is not rejection."""
    from datetime import datetime, timezone
    from .analysis.decisions import evaluate
    facts_profile = profile.model_copy(update={"roles": [], "skills": [], "preferred_cities": []})
    return evaluate(row, facts_profile, datetime.now(timezone.utc))["bucket"] != "暂不优先"
