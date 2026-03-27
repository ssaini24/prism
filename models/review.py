from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class Issue(BaseModel):
    type: str
    severity: Literal["low", "medium", "high"]
    confidence: Literal["low", "medium", "high"]
    line: int = 0
    description: str
    suggestion: str


class CostAnalysis(BaseModel):
    level: Literal["low", "medium", "high"]
    basis: Literal["static", "explain", "runtime"]
    reason: str
    estimated_improvement: str = ""


class ReviewResult(BaseModel):
    issues: list[Issue] = Field(default_factory=list)
    optimized_query: str = ""
    index_suggestions: list[str] = Field(default_factory=list)
    migration_warnings: list[str] = Field(default_factory=list)
    cost_analysis: CostAnalysis = Field(
        default_factory=lambda: CostAnalysis(
            level="low", basis="static", reason="No issues detected."
        )
    )
    explanation: str = ""
    suppressed: list[str] = Field(default_factory=list)


class ExtractedQuery(BaseModel):
    raw: str
    file: str
    line: int
    suppressed: bool = False
