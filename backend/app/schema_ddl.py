"""Single source of truth for the SQLite schema.

Implements roadmap Part 8 / research section 15 (``15_database_schema.md``).

Conventions (section 15 §Global conventions):
- TEXT UUIDv4 primary keys on all domain tables.
- Timestamps are INTEGER Unix epoch **milliseconds** (columns keep the
  section-15 names: ``created_at``, ``updated_at``, ``run_after``, ...;
  only ``event_log`` uses the explicit ``created_at_ms`` name).
- Booleans are INTEGER 0/1. Flexible payloads live in ``*_json`` TEXT columns.
- Money is ``cost_micros`` INTEGER (USD x 1,000,000) — never floats.
- Enums are TEXT + CHECK. Soft delete (``deleted_at``) only on user-facing
  entities (channels, projects, schedules).
- FK policy: ON DELETE RESTRICT for audit-linked parents; CASCADE only for
  true owned children (project_artefacts, pipeline_checkpoints,
  asset_licences, project_assets).
- Secrets are NEVER stored here: ``provider_credentials`` is metadata only
  (``keyring_ref`` points at Credential Manager / DPAPI storage).

The DDL is kept as a list of individual statements (``DDL_STATEMENTS``) so
both ``sqlite3.executescript`` (app/db.py) and Alembic (one ``op.execute``
per statement) can consume exactly the same definitions. ``SCHEMA_DDL`` is
the joined script form.
"""

from __future__ import annotations

SCHEMA_VERSION = 1

# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------

_TABLE_STATEMENTS: list[str] = [
    # -- schema bookkeeping -------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS schema_meta (
        id         INTEGER PRIMARY KEY CHECK (id = 1),
        version    INTEGER NOT NULL,
        updated_at INTEGER NOT NULL
    )
    """,
    # -- identity, settings, credentials ------------------------------------
    """
    CREATE TABLE IF NOT EXISTS users (
        id          TEXT PRIMARY KEY,
        display_name TEXT NOT NULL,
        email       TEXT NULL,
        locale      TEXT NOT NULL DEFAULT 'en-US'
                    CHECK (locale IN ('ur-PK', 'en-US')),
        timezone    TEXT NOT NULL DEFAULT 'UTC',
        created_at  INTEGER NOT NULL,
        updated_at  INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS settings (
        key        TEXT PRIMARY KEY,
        value_json TEXT NOT NULL,
        updated_at INTEGER NOT NULL
    )
    """,
    # METADATA ONLY — the secret itself lives in Credential Manager / DPAPI,
    # referenced by keyring_ref. There is deliberately NO column that could
    # hold a secret value (no api_key / token / secret / password column).
    """
    CREATE TABLE IF NOT EXISTS provider_credentials (
        id               TEXT PRIMARY KEY,
        provider         TEXT NOT NULL
                         CHECK (provider IN ('youtube', 'gemini', 'openai',
                                'anthropic', 'elevenlabs', 'pexels',
                                'pixabay', 'image_gen')),
        credential_kind  TEXT NOT NULL
                         CHECK (credential_kind IN ('api_key', 'oauth')),
        keyring_ref      TEXT NOT NULL,
        account_label    TEXT NULL,
        scopes           TEXT NULL,
        status           TEXT NOT NULL DEFAULT 'missing'
                         CHECK (status IN ('active', 'expired', 'revoked',
                                'missing')),
        last_validated_at INTEGER NULL,
        created_at       INTEGER NOT NULL,
        updated_at       INTEGER NOT NULL,
        UNIQUE (provider, account_label)
    )
    """,
    # -- channels & niches ---------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS niche_profiles (
        id               TEXT PRIMARY KEY,
        key              TEXT NOT NULL UNIQUE,
        label            TEXT NOT NULL,
        source           TEXT NOT NULL
                         CHECK (source IN ('builtin', 'other')),
        user_idea        TEXT NULL,
        language_mix     TEXT NULL,
        tone             TEXT NULL,
        words_per_minute INTEGER NULL,
        pace             TEXT NULL
                         CHECK (pace IN ('slow', 'medium', 'fast')),
        structure_json   TEXT NULL,
        hook_style       TEXT NULL,
        voice_style      TEXT NULL,
        default_voice_id TEXT NULL,
        visual_style     TEXT NULL,
        cta_style        TEXT NULL,
        risk_class       TEXT NOT NULL DEFAULT 'normal'
                         CHECK (risk_class IN ('normal', 'sensitive')),
        taboo_json       TEXT NULL,
        is_active        INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
        created_at       INTEGER NOT NULL,
        updated_at       INTEGER NOT NULL,
        CHECK (user_idea IS NULL OR source = 'other')
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS channels (
        id                       TEXT PRIMARY KEY,
        youtube_channel_id       TEXT NOT NULL UNIQUE,
        title                    TEXT NOT NULL,
        handle                   TEXT NULL,
        credential_id            TEXT NULL
                                 REFERENCES provider_credentials (id)
                                 ON DELETE RESTRICT,
        default_niche_profile_id TEXT NULL
                                 REFERENCES niche_profiles (id)
                                 ON DELETE RESTRICT,
        language                 TEXT NOT NULL,
        country                  TEXT NULL,
        status                   TEXT NOT NULL DEFAULT 'active'
                                 CHECK (status IN ('active', 'paused',
                                        'disconnected', 'at_risk')),
        daily_upload_cap         INTEGER NULL,
        monthly_budget_micros    INTEGER NULL,
        quality_tier             TEXT NOT NULL DEFAULT 'draft'
                                 CHECK (quality_tier IN ('draft',
                                        'master_1080p',
                                        'master_1440p_upscale')),
        config_json              TEXT NULL,
        created_at               INTEGER NOT NULL,
        updated_at               INTEGER NOT NULL,
        deleted_at               INTEGER NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS schedules (
        id                    TEXT PRIMARY KEY,
        name                  TEXT NOT NULL,
        channel_id            TEXT NOT NULL
                              REFERENCES channels (id) ON DELETE RESTRICT,
        niche_profile_id      TEXT NOT NULL
                              REFERENCES niche_profiles (id)
                              ON DELETE RESTRICT,
        cadence_json          TEXT NULL,
        topic_mode            TEXT NOT NULL DEFAULT 'auto'
                              CHECK (topic_mode IN ('auto', 'queue',
                                     'fixed_list')),
        target_minutes        INTEGER NULL,
        publish_mode          TEXT NOT NULL DEFAULT 'review_first'
                              CHECK (publish_mode IN ('auto', 'review_first')),
        daily_cost_cap_micros INTEGER NULL,
        is_enabled            INTEGER NOT NULL DEFAULT 1
                              CHECK (is_enabled IN (0, 1)),
        next_run_at           INTEGER NULL,
        last_run_at           INTEGER NULL,
        created_at            INTEGER NOT NULL,
        updated_at            INTEGER NOT NULL,
        deleted_at            INTEGER NULL
    )
    """,
    # -- pipeline core --------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS projects (
        id                TEXT PRIMARY KEY,
        channel_id        TEXT NOT NULL
                          REFERENCES channels (id) ON DELETE RESTRICT,
        niche_profile_id  TEXT NOT NULL
                          REFERENCES niche_profiles (id) ON DELETE RESTRICT,
        topic             TEXT NOT NULL,
        topic_source      TEXT NOT NULL
                          CHECK (topic_source IN ('agent', 'user',
                                 'schedule')),
        target_minutes    INTEGER NULL,
        target_words      INTEGER NULL,
        status            TEXT NOT NULL DEFAULT 'draft'
                          CHECK (status IN ('draft', 'in_production',
                                 'awaiting_review', 'qa', 'ready',
                                 'scheduled', 'published', 'failed',
                                 'blocked', 'cancelled')),
        current_phase     TEXT NULL
                          CHECK (current_phase IN ('research', 'scripting',
                                 'production', 'editing', 'packaging',
                                 'publishing', 'growth')),
        gate_status_json  TEXT NULL,
        publish_mode      TEXT NOT NULL DEFAULT 'review_first'
                          CHECK (publish_mode IN ('auto', 'review_first')),
        schedule_id       TEXT NULL
                          REFERENCES schedules (id) ON DELETE SET NULL,
        youtube_video_id  TEXT NULL UNIQUE,
        published_at      INTEGER NULL,
        total_cost_micros INTEGER NOT NULL DEFAULT 0,
        error_summary     TEXT NULL,
        created_at        INTEGER NOT NULL,
        updated_at        INTEGER NOT NULL,
        deleted_at        INTEGER NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS jobs (
        id               TEXT PRIMARY KEY,
        project_id       TEXT NULL
                         REFERENCES projects (id) ON DELETE RESTRICT,
        agent_key        TEXT NOT NULL,
        kind             TEXT NOT NULL,
        payload_json     TEXT NULL,
        status           TEXT NOT NULL DEFAULT 'queued'
                         CHECK (status IN ('queued', 'leased', 'running',
                                'done', 'failed', 'dead', 'cancelled')),
        priority         INTEGER NOT NULL DEFAULT 0,
        depends_on_job_id TEXT NULL
                         REFERENCES jobs (id) ON DELETE RESTRICT,
        run_after        INTEGER NOT NULL DEFAULT 0,
        attempts         INTEGER NOT NULL DEFAULT 0,
        max_attempts     INTEGER NOT NULL DEFAULT 3,
        lease_owner      TEXT NULL,
        lease_expires_at INTEGER NULL,
        heartbeat_at     INTEGER NULL,
        idempotency_key  TEXT NULL UNIQUE,
        result_json      TEXT NULL,
        error            TEXT NULL,
        error_code       TEXT NULL,
        created_at       INTEGER NOT NULL,
        updated_at       INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS agent_runs (
        id              TEXT PRIMARY KEY,
        job_id          TEXT NOT NULL
                        REFERENCES jobs (id) ON DELETE RESTRICT,
        project_id      TEXT NULL
                        REFERENCES projects (id) ON DELETE RESTRICT,
        agent_key       TEXT NOT NULL,
        attempt         INTEGER NOT NULL DEFAULT 1,
        status          TEXT NOT NULL DEFAULT 'running'
                        CHECK (status IN ('running', 'succeeded', 'failed',
                               'blocked', 'skipped')),
        model           TEXT NULL,
        provider        TEXT NULL,
        input_ref_json  TEXT NULL,
        output_ref_json TEXT NULL,
        decision_log    TEXT NULL,
        tokens_in       INTEGER NULL,
        tokens_out      INTEGER NULL,
        cost_micros     INTEGER NOT NULL DEFAULT 0,
        error           TEXT NULL,
        started_at      INTEGER NULL,
        finished_at     INTEGER NULL,
        created_at      INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS project_artefacts (
        id                 TEXT PRIMARY KEY,
        project_id         TEXT NOT NULL
                           REFERENCES projects (id) ON DELETE CASCADE,
        artefact_type      TEXT NOT NULL
                           CHECK (artefact_type IN ('research_brief',
                                  'outline', 'script', 'voiceover_text',
                                  'audio', 'scene_list', 'rough_cut',
                                  'master_video', 'subtitle', 'thumbnail',
                                  'metadata_pack', 'qc_report')),
        version            INTEGER NOT NULL,
        file_path          TEXT NOT NULL,
        content_hash       TEXT NOT NULL,
        is_locked          INTEGER NOT NULL DEFAULT 0
                           CHECK (is_locked IN (0, 1)),
        produced_by_run_id TEXT NULL
                           REFERENCES agent_runs (id) ON DELETE RESTRICT,
        supersedes_id      TEXT NULL
                           REFERENCES project_artefacts (id)
                           ON DELETE RESTRICT,
        created_at         INTEGER NOT NULL,
        UNIQUE (project_id, artefact_type, version)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS pipeline_checkpoints (
        project_id TEXT NOT NULL
                   REFERENCES projects (id) ON DELETE CASCADE,
        step_key   TEXT NOT NULL,
        state_blob BLOB NOT NULL,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NULL,
        PRIMARY KEY (project_id, step_key)
    )
    """,
    # -- assets & licences -----------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS assets (
        id             TEXT PRIMARY KEY,
        content_hash   TEXT NOT NULL UNIQUE,
        asset_type     TEXT NOT NULL
                       CHECK (asset_type IN ('stock_video', 'stock_image',
                              'ai_image', 'music', 'sfx', 'voice_audio',
                              'font', 'other')),
        file_path      TEXT NOT NULL,
        source_provider TEXT NOT NULL,
        source_url     TEXT NULL,
        duration_ms    INTEGER NULL,
        width          INTEGER NULL,
        height         INTEGER NULL,
        created_at     INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS asset_licences (
        asset_id            TEXT PRIMARY KEY
                            REFERENCES assets (id) ON DELETE CASCADE,
        licence_type        TEXT NOT NULL
                            CHECK (licence_type IN ('pexels_free',
                                   'pixabay_content', 'cc0',
                                   'paid_royalty_free', 'user_owned')),
        licence_url         TEXT NULL,
        attribution_required INTEGER NOT NULL DEFAULT 0
                            CHECK (attribution_required IN (0, 1)),
        commercial_use_ok   INTEGER NOT NULL DEFAULT 0
                            CHECK (commercial_use_ok IN (0, 1)),
        verified_at         INTEGER NULL,
        notes               TEXT NULL
    )
    """,
    # NOTE: section 15 models this as composite PK (project_id, asset_id,
    # scene_no) with scene_no NULL meaning "non-scene use" — a nullable PK
    # column cannot enforce uniqueness in SQLite, so uniqueness is enforced
    # by the expression index uq_project_assets below using
    # COALESCE(scene_no, -1). -1 is never a real scene number.
    """
    CREATE TABLE IF NOT EXISTS project_assets (
        project_id TEXT NOT NULL
                   REFERENCES projects (id) ON DELETE CASCADE,
        asset_id   TEXT NOT NULL
                   REFERENCES assets (id) ON DELETE CASCADE,
        scene_no   INTEGER NULL,
        usage_role TEXT NOT NULL
                   CHECK (usage_role IN ('scene_visual', 'thumbnail_bg',
                          'music', 'sfx', 'overlay')),
        created_at INTEGER NOT NULL
    )
    """,
    # -- usage, cost & quota ledger (append-only) ------------------------------
    """
    CREATE TABLE IF NOT EXISTS api_usage (
        id           TEXT PRIMARY KEY,
        provider     TEXT NOT NULL,
        operation    TEXT NOT NULL,
        project_id   TEXT NULL
                     REFERENCES projects (id) ON DELETE RESTRICT,
        agent_run_id TEXT NULL
                     REFERENCES agent_runs (id) ON DELETE RESTRICT,
        units        INTEGER NULL,
        tokens_in    INTEGER NULL,
        tokens_out   INTEGER NULL,
        characters   INTEGER NULL,
        images       INTEGER NULL,
        cost_micros  INTEGER NOT NULL DEFAULT 0,
        status       TEXT NOT NULL DEFAULT 'ok'
                     CHECK (status IN ('ok', 'rate_limited', 'error')),
        created_at   INTEGER NOT NULL
    )
    """,
    # -- scheduling --------------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS schedule_runs (
        id          TEXT PRIMARY KEY,
        schedule_id TEXT NOT NULL
                    REFERENCES schedules (id) ON DELETE RESTRICT,
        project_id  TEXT NULL
                    REFERENCES projects (id) ON DELETE RESTRICT,
        status      TEXT NOT NULL
                    CHECK (status IN ('created', 'skipped_cap',
                           'skipped_disabled', 'failed')),
        reason      TEXT NULL,
        detail_json TEXT NULL,
        started_at  INTEGER NULL,
        finished_at INTEGER NULL,
        created_at  INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS topic_queue (
        id                TEXT PRIMARY KEY,
        channel_id        TEXT NULL
                          REFERENCES channels (id) ON DELETE RESTRICT,
        niche_profile_id  TEXT NOT NULL
                          REFERENCES niche_profiles (id) ON DELETE RESTRICT,
        topic             TEXT NOT NULL,
        source            TEXT NOT NULL
                          CHECK (source IN ('user', 'agent_suggested',
                                 'analytics_feedback')),
        priority          INTEGER NOT NULL DEFAULT 0,
        status            TEXT NOT NULL DEFAULT 'pending'
                          CHECK (status IN ('pending', 'used', 'rejected')),
        used_in_project_id TEXT NULL
                          REFERENCES projects (id) ON DELETE RESTRICT,
        created_at        INTEGER NOT NULL
    )
    """,
    # -- analytics & learning stores ---------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS analytics_snapshots (
        id                    TEXT PRIMARY KEY,
        project_id            TEXT NULL
                              REFERENCES projects (id) ON DELETE RESTRICT,
        channel_id            TEXT NOT NULL
                              REFERENCES channels (id) ON DELETE RESTRICT,
        youtube_video_id      TEXT NULL,
        snapshot_at           INTEGER NOT NULL,
        views                 INTEGER NOT NULL DEFAULT 0,
        watch_hours_x100      INTEGER NOT NULL DEFAULT 0,
        likes                 INTEGER NOT NULL DEFAULT 0,
        comments_count        INTEGER NOT NULL DEFAULT 0,
        shares                INTEGER NOT NULL DEFAULT 0,
        ctr_bps               INTEGER NULL,
        avg_view_duration_sec INTEGER NULL,
        retention_curve_json  TEXT NULL,
        subs_delta            INTEGER NULL,
        revenue_micros        INTEGER NULL,
        source                TEXT NOT NULL DEFAULT 'youtube_analytics_api',
        created_at            INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS performance_insights (
        id               TEXT PRIMARY KEY,
        scope            TEXT NOT NULL
                         CHECK (scope IN ('channel', 'niche', 'global')),
        channel_id       TEXT NULL
                         REFERENCES channels (id) ON DELETE RESTRICT,
        niche_profile_id TEXT NULL
                         REFERENCES niche_profiles (id) ON DELETE RESTRICT,
        insight_type     TEXT NOT NULL
                         CHECK (insight_type IN ('winning_topic_pattern',
                                'winning_hook', 'title_pattern',
                                'thumbnail_pattern', 'pacing', 'avoid')),
        insight_text     TEXT NOT NULL,
        evidence_json    TEXT NULL,
        confidence       REAL NOT NULL DEFAULT 0,
        status           TEXT NOT NULL DEFAULT 'active'
                         CHECK (status IN ('active', 'superseded',
                                'retired')),
        created_at       INTEGER NOT NULL,
        updated_at       INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS used_topics (
        id               TEXT PRIMARY KEY,
        channel_id       TEXT NOT NULL
                         REFERENCES channels (id) ON DELETE RESTRICT,
        topic_normalized TEXT NOT NULL,
        topic_hash       TEXT NOT NULL,
        project_id       TEXT NOT NULL
                         REFERENCES projects (id) ON DELETE RESTRICT,
        used_at          INTEGER NOT NULL,
        UNIQUE (channel_id, topic_hash)
    )
    """,
    # NOTE: UNIQUE(niche_profile_id, term_normalized) is enforced via the
    # expression index uq_pronunciation_lookup below because SQLite UNIQUE
    # treats NULL niche_profile_id (= global rows) as distinct.
    """
    CREATE TABLE IF NOT EXISTS pronunciation_dictionary (
        id               TEXT PRIMARY KEY,
        niche_profile_id TEXT NULL
                         REFERENCES niche_profiles (id) ON DELETE RESTRICT,
        term             TEXT NOT NULL,
        term_normalized  TEXT NOT NULL,
        phonetic         TEXT NOT NULL,
        language         TEXT NULL,
        source           TEXT NOT NULL
                         CHECK (source IN ('agent_fix', 'user')),
        created_at       INTEGER NOT NULL,
        updated_at       INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS cross_video_similarity (
        id                    TEXT PRIMARY KEY,
        project_id            TEXT NOT NULL
                              REFERENCES projects (id) ON DELETE RESTRICT,
        compared_project_id   TEXT NOT NULL
                              REFERENCES projects (id) ON DELETE RESTRICT,
        script_similarity     REAL NULL,
        structure_similarity  REAL NULL,
        visual_similarity     REAL NULL,
        verdict               TEXT NOT NULL
                              CHECK (verdict IN ('pass', 'warn', 'fail')),
        created_at            INTEGER NOT NULL,
        CHECK (project_id <> compared_project_id)
    )
    """,
    # -- provenance & publish records ----------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS provenance_log (
        id           TEXT PRIMARY KEY,
        project_id   TEXT NOT NULL
                     REFERENCES projects (id) ON DELETE RESTRICT,
        event_type   TEXT NOT NULL
                     CHECK (event_type IN ('research_source', 'prompt_used',
                            'asset_acquired', 'script_generated',
                            'disclosure_decision', 'licence_verified',
                            'publish', 'claim_received', 'appeal_filed')),
        detail_json  TEXT NULL,
        agent_run_id TEXT NULL
                     REFERENCES agent_runs (id) ON DELETE RESTRICT,
        created_at   INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS publish_records (
        id                        TEXT PRIMARY KEY,
        project_id                TEXT NOT NULL UNIQUE
                                  REFERENCES projects (id)
                                  ON DELETE RESTRICT,
        youtube_video_id          TEXT NOT NULL UNIQUE,
        video_url                 TEXT NULL,
        title                     TEXT NULL,
        description_hash          TEXT NULL,
        tags_json                 TEXT NULL,
        thumbnail_artefact_id     TEXT NULL
                                  REFERENCES project_artefacts (id)
                                  ON DELETE RESTRICT,
        privacy_status            TEXT NOT NULL DEFAULT 'private'
                                  CHECK (privacy_status IN ('public',
                                         'unlisted', 'private',
                                         'scheduled')),
        scheduled_for             INTEGER NULL,
        disclosure_altered_content INTEGER NULL
                                  CHECK (disclosure_altered_content
                                         IN (0, 1)),
        upload_idempotency_key    TEXT NOT NULL UNIQUE,
        published_at              INTEGER NULL,
        created_at                INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS notifications (
        id         TEXT PRIMARY KEY,
        project_id TEXT NULL
                   REFERENCES projects (id) ON DELETE RESTRICT,
        kind       TEXT NOT NULL
                   CHECK (kind IN ('gate_review', 'blocked', 'error',
                          'daily_report', 'quota_warning', 'policy_risk')),
        title      TEXT NOT NULL,
        body       TEXT NULL,
        is_read    INTEGER NOT NULL DEFAULT 0 CHECK (is_read IN (0, 1)),
        created_at INTEGER NOT NULL
    )
    """,
    # -- WebSocket event journal (reconnect replay) ----------------------------------
    """
    CREATE TABLE IF NOT EXISTS event_log (
        id            TEXT PRIMARY KEY,
        seq           INTEGER NOT NULL,
        boot_id       TEXT NOT NULL,
        type          TEXT NOT NULL,
        payload_json  TEXT NULL,
        created_at_ms INTEGER NOT NULL,
        UNIQUE (boot_id, seq)
    )
    """,
]

# ---------------------------------------------------------------------------
# Indexes — section 15 §Indexing plan (Q1–Q16), each justified by a hot query
# ---------------------------------------------------------------------------

_INDEX_STATEMENTS: list[str] = [
    # Q1 queue claiming — partial, only live jobs
    """
    CREATE INDEX IF NOT EXISTS idx_jobs_claim
    ON jobs (status, priority DESC, run_after, created_at)
    WHERE status IN ('queued', 'leased')
    """,
    # Q2 dependency release
    """
    CREATE INDEX IF NOT EXISTS idx_jobs_depends
    ON jobs (depends_on_job_id)
    WHERE status = 'queued'
    """,
    # Q3 lease recovery sweep
    """
    CREATE INDEX IF NOT EXISTS idx_jobs_lease
    ON jobs (lease_expires_at)
    WHERE status IN ('leased', 'running')
    """,
    # Q4 dead/blocked jobs for UI + escalation; History filters
    """
    CREATE INDEX IF NOT EXISTS idx_jobs_status_time
    ON jobs (status, updated_at DESC)
    """,
    # Q5 project timeline
    """
    CREATE INDEX IF NOT EXISTS idx_runs_project
    ON agent_runs (project_id, started_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_runs_job
    ON agent_runs (job_id)
    """,
    # Q6 library / history
    """
    CREATE INDEX IF NOT EXISTS idx_projects_channel
    ON projects (channel_id, status, created_at DESC)
    WHERE deleted_at IS NULL
    """,
    # Q7 live dashboard
    """
    CREATE INDEX IF NOT EXISTS idx_projects_status
    ON projects (status, current_phase)
    WHERE status IN ('in_production', 'qa', 'awaiting_review')
    """,
    # Q8 quota / cost meters
    """
    CREATE INDEX IF NOT EXISTS idx_usage_provider_time
    ON api_usage (provider, created_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_usage_project
    ON api_usage (project_id, created_at)
    """,
    # Q9 scheduler tick
    """
    CREATE INDEX IF NOT EXISTS idx_schedules_next
    ON schedules (next_run_at)
    WHERE is_enabled = 1
    """,
    # Q13 analytics deltas
    """
    CREATE INDEX IF NOT EXISTS idx_snapshots_video
    ON analytics_snapshots (youtube_video_id, snapshot_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_snapshots_channel
    ON analytics_snapshots (channel_id, snapshot_at)
    """,
    # Q14 learning read at job start
    """
    CREATE INDEX IF NOT EXISTS idx_insights_lookup
    ON performance_insights (scope, niche_profile_id, channel_id)
    WHERE status = 'active'
    """,
    # Q15 provenance export / audit
    """
    CREATE INDEX IF NOT EXISTS idx_provenance_project
    ON provenance_log (project_id, created_at)
    """,
    # Q16 artefact resolution ("current locked script for project X")
    """
    CREATE INDEX IF NOT EXISTS idx_artefacts_current
    ON project_artefacts (project_id, artefact_type, version DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_artefacts_locked
    ON project_artefacts (project_id, artefact_type, version DESC)
    WHERE is_locked = 1
    """,
    # project_assets uniqueness incl. NULL scene_no (see table note above)
    """
    CREATE UNIQUE INDEX IF NOT EXISTS uq_project_assets
    ON project_assets (project_id, asset_id, COALESCE(scene_no, -1))
    """,
    # Q11 pronunciation lookup incl. global (NULL niche) rows
    """
    CREATE UNIQUE INDEX IF NOT EXISTS uq_pronunciation_lookup
    ON pronunciation_dictionary (COALESCE(niche_profile_id, ''),
                                 term_normalized)
    """,
    # WebSocket replay: events of one boot in seq order
    """
    CREATE INDEX IF NOT EXISTS idx_event_log_boot_seq
    ON event_log (boot_id, seq)
    """,
]

# ---------------------------------------------------------------------------
# Triggers — provenance_log is append-only (section 15: enforced by TRIGGER)
# RAISE(ABORT) surfaces in Python as sqlite3.IntegrityError.
# ---------------------------------------------------------------------------

_TRIGGER_STATEMENTS: list[str] = [
    """
    CREATE TRIGGER IF NOT EXISTS trg_provenance_log_no_update
    BEFORE UPDATE ON provenance_log
    BEGIN
        SELECT RAISE(ABORT, 'provenance_log is append-only');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_provenance_log_no_delete
    BEFORE DELETE ON provenance_log
    BEGIN
        SELECT RAISE(ABORT, 'provenance_log is append-only');
    END
    """,
]

DDL_STATEMENTS: list[str] = (
    _TABLE_STATEMENTS + _INDEX_STATEMENTS + _TRIGGER_STATEMENTS
)

SCHEMA_DDL: str = ";\n".join(DDL_STATEMENTS) + ";\n"

# All application tables (excludes SQLite internals and alembic_version).
TABLE_NAMES: list[str] = [
    "schema_meta",
    "users",
    "settings",
    "provider_credentials",
    "niche_profiles",
    "channels",
    "schedules",
    "projects",
    "jobs",
    "agent_runs",
    "project_artefacts",
    "pipeline_checkpoints",
    "assets",
    "asset_licences",
    "project_assets",
    "api_usage",
    "schedule_runs",
    "topic_queue",
    "analytics_snapshots",
    "performance_insights",
    "used_topics",
    "pronunciation_dictionary",
    "cross_video_similarity",
    "provenance_log",
    "publish_records",
    "notifications",
    "event_log",
]

# Children first, so DROP TABLE succeeds with foreign_keys=ON.
DROP_ORDER: list[str] = [
    "event_log",
    "notifications",
    "publish_records",
    "provenance_log",
    "cross_video_similarity",
    "pronunciation_dictionary",
    "used_topics",
    "performance_insights",
    "analytics_snapshots",
    "topic_queue",
    "schedule_runs",
    "api_usage",
    "project_assets",
    "asset_licences",
    "assets",
    "pipeline_checkpoints",
    "agent_runs",
    "project_artefacts",
    "jobs",
    "projects",
    "schedules",
    "channels",
    "niche_profiles",
    "provider_credentials",
    "settings",
    "users",
    "schema_meta",
]
