"""Style Lab (competitor analysis) — intake, storage, analysis, profiles.

This package implements the Style Lab mini-round: a user supplies
competitor YouTube channels / videos, the app analyses them and saves a
*style profile* scoped to one of the user's own channels.

Submodules:

* :mod:`app.style_lab.schema` — DDL for ``competitor_channels``,
  ``competitor_videos`` and ``style_profiles`` (sibling-owned).
* :mod:`app.style_lab.parsing` — pure parsing of user-supplied
  competitor inputs (URLs, handles, channel ids) into a
  :class:`~app.style_lab.parsing.ParsedCompetitor`.
* :mod:`app.style_lab.store` — the SQLite store (CRUD, dedupe,
  owner-channel scoping) over those tables.

HARD BOUNDARY — style / formula-level learning only: structure,
pacing, hook patterns, stock-vs-AI visual mix, title / description
patterns.  Style Lab never reuses a competitor's footage, script
text, exact titles, thumbnails or branding (copyright, YouTube's
reused-content policy, and the app's own similarity gate).  Whether a
scene looks stock or AI-generated is probabilistic and is always
reported as a likelihood, never as a fact.
"""
