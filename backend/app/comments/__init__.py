"""Comments Intelligence (Round 5 — Agent 28): schema + storage.

Public surface for the rest of the Round 5 team:

* :mod:`app.comments.schema` — DDL constants +
  :func:`ensure_comments_schema` (byte-identical with migration 0006).
* :mod:`app.comments.store` — :class:`CommentsStore`, the
  owner-scoped store every comments worker should go through.
"""

from __future__ import annotations

from app.comments.schema import ensure_comments_schema
from app.comments.store import (
    ANALYSIS_ENGINES,
    ANALYSIS_SCOPES,
    DRAFT_STATUSES,
    FLAG_KINDS,
    FLAG_STATUSES,
    SYNC_STATUSES,
    CommentsNotFoundError,
    CommentsScopeError,
    CommentsStateError,
    CommentsStore,
    CommentsStoreError,
)

__all__ = [
    "ensure_comments_schema",
    "CommentsStore",
    "CommentsStoreError",
    "CommentsNotFoundError",
    "CommentsScopeError",
    "CommentsStateError",
    "SYNC_STATUSES",
    "ANALYSIS_SCOPES",
    "ANALYSIS_ENGINES",
    "DRAFT_STATUSES",
    "FLAG_KINDS",
    "FLAG_STATUSES",
]
