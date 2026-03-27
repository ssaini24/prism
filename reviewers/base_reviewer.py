"""Abstract base class that every reviewer module must implement."""
from __future__ import annotations

from abc import ABC, abstractmethod

from models.review import ExtractedQuery, ReviewResult


class BaseReviewer(ABC):
    """
    All reviewer modules inherit from this class.

    A reviewer receives one extracted query at a time and returns a
    ReviewResult. The analyser orchestrates which reviewer(s) to invoke.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable reviewer name (used in PR comment headings)."""

    @abstractmethod
    def can_review(self, query: ExtractedQuery) -> bool:
        """
        Return True if this reviewer is applicable to the given query.

        Allows reviewers to opt out for unsupported dialects, file types, etc.
        """

    @abstractmethod
    def review(self, query: ExtractedQuery, schema_context: str = "") -> ReviewResult:
        """
        Analyse the query and return a structured ReviewResult.

        Args:
            query: The extracted query with file/line metadata.
            schema_context: Optional DDL / migration context as a string.
        """
