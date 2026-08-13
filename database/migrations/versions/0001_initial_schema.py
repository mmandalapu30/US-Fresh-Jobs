"""Initial schema: companies, jobs, lifecycle, provenance, sync tracking, users.

Revision ID: 0001
Revises:
Create Date: 2026-08-10

Design notes that are load-bearing (see docs/00-source-verification.md):

* Seven distinct timestamp columns on ``jobs``. ``posted_at`` (source truth, 19% NULL,
  can be in the future) is never overwritten by ``first_seen_at`` (our clock).
* ``posted_at_is_valid`` is a stored, generated-ish flag maintained by the pipeline so
  the hot feed index can exclude future-dated rows cheaply via a partial index.
* ``job_sources`` carries provenance N:1 to ``jobs`` -- when two sources describe the same
  job we keep both rows and one canonical job.
* ``job_events`` and ``job_snapshots`` are RANGE-partitioned by month: append-only, highest
  growth, always queried by time window.
* ``jobs`` is deliberately NOT partitioned in v1; the partition key would have to join every
  unique constraint and would break dedupe upserts. Revisit at ~50M rows.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


# Months pre-created for the partitioned tables. A Celery beat task
# (``ensure_future_partitions``, Milestone 8) keeps a rolling window ahead of "now"; this
# initial set makes a fresh database immediately writable.
_INITIAL_PARTITION_MONTHS = [
    (2026, 1), (2026, 2), (2026, 3), (2026, 4), (2026, 5), (2026, 6),
    (2026, 7), (2026, 8), (2026, 9), (2026, 10), (2026, 11), (2026, 12),
    (2027, 1), (2027, 2), (2027, 3),
]


def _month_bounds(year: int, month: int) -> tuple[str, str]:
    start = f"{year:04d}-{month:02d}-01"
    end = f"{year + 1:04d}-01-01" if month == 12 else f"{year:04d}-{month + 1:02d}-01"
    return start, end


def upgrade() -> None:
    # ------------------------------------------------------------------ extensions
    # pg_trgm powers company/title autocomplete; citext gives case-insensitive email
    # uniqueness without a functional index everyone forgets to use.
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute("CREATE EXTENSION IF NOT EXISTS citext")

    # ----------------------------------------------------------------------- enums
    op.execute(
        "CREATE TYPE job_status AS ENUM ('ACTIVE', 'EXPIRED', 'REMOVED', 'UNKNOWN')"
    )
    op.execute(
        "CREATE TYPE remote_type AS ENUM ('REMOTE', 'HYBRID', 'ONSITE', 'UNKNOWN')"
    )
    op.execute(
        "CREATE TYPE employment_type AS ENUM ('FULL_TIME', 'PART_TIME', 'CONTRACT', "
        "'TEMPORARY', 'INTERNSHIP', 'VOLUNTEER', 'OTHER', 'UNKNOWN')"
    )
    op.execute(
        "CREATE TYPE salary_interval AS ENUM ('HOURLY', 'DAILY', 'WEEKLY', 'MONTHLY', "
        "'ANNUAL', 'UNKNOWN')"
    )
    op.execute(
        "CREATE TYPE job_event_type AS ENUM ('CREATED', 'UPDATED', 'REPOSTED', 'EXPIRED', "
        "'REMOVED', 'REACTIVATED', 'MERGED', 'QUARANTINED')"
    )
    op.execute(
        "CREATE TYPE sync_status AS ENUM ('PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED', "
        "'PARTIAL', 'CANCELLED')"
    )
    op.execute(
        "CREATE TYPE sync_trigger AS ENUM ('SCHEDULED', 'MANUAL', 'BACKFILL', 'RETRY')"
    )
    op.execute("CREATE TYPE user_role AS ENUM ('USER', 'ADMIN', 'SERVICE')")
    op.execute(
        "CREATE TYPE alert_frequency AS ENUM ('IMMEDIATE', 'HOURLY', 'DAILY', 'WEEKLY')"
    )
    op.execute("CREATE TYPE alert_channel AS ENUM ('EMAIL', 'PUSH', 'SMS')")
    op.execute(
        "CREATE TYPE dedupe_level AS ENUM ('L1_SOURCE_ID', 'L2_APPLY_URL', "
        "'L3_COMPANY_TITLE_LOCATION', 'L4_CONTENT_FINGERPRINT', 'NONE')"
    )

    # ------------------------------------------------------------ shared updated_at
    op.execute(
        """
        CREATE OR REPLACE FUNCTION set_updated_at()
        RETURNS TRIGGER AS $$
        BEGIN
            NEW.updated_at = now();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )

    # ------------------------------------------------------------------- companies
    op.execute(
        """
        CREATE TABLE companies (
            id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            name                TEXT        NOT NULL,
            name_normalized     TEXT        NOT NULL,
            website             TEXT,
            domain              TEXT,
            ats                 TEXT,
            slug                TEXT,
            career_url          TEXT,
            founded_year        INTEGER,
            size_range          TEXT,
            industry            TEXT,
            hq_city             TEXT,
            hq_region           TEXT,
            hq_country_code     CHAR(2),
            linkedin_url        TEXT,
            linkedin_id         TEXT,
            logo_url            TEXT,
            created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT companies_founded_year_sane
                CHECK (founded_year IS NULL OR founded_year BETWEEN 1600 AND 2200)
        )
        """
    )
    op.create_index("companies_name_normalized_idx", "companies", ["name_normalized"])
    op.execute(
        "CREATE INDEX companies_name_trgm_idx ON companies "
        "USING GIN (name_normalized gin_trgm_ops)"
    )
    op.execute(
        "CREATE UNIQUE INDEX companies_domain_uq ON companies (domain) "
        "WHERE domain IS NOT NULL"
    )
    op.execute(
        "CREATE TRIGGER companies_set_updated_at BEFORE UPDATE ON companies "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )

    # Provenance for companies: the same firm can arrive from several sources with
    # different upstream ids. Mirrors job_sources.
    op.execute(
        """
        CREATE TABLE company_sources (
            id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            company_id          BIGINT      NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
            source              TEXT        NOT NULL,
            external_company_id TEXT        NOT NULL,
            source_unique_id    TEXT,
            first_seen_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
            last_seen_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
            raw_payload         JSONB,
            CONSTRAINT company_sources_uq UNIQUE (source, external_company_id)
        )
        """
    )
    op.create_index("company_sources_company_id_idx", "company_sources", ["company_id"])

    # ------------------------------------------------------------- job_locations
    # Normalized location dimension. Denormalized copies live on `jobs` so the hot feed
    # never needs this join; this table exists for /locations pages and analytics.
    op.execute(
        """
        CREATE TABLE job_locations (
            id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            country_code        CHAR(2)     NOT NULL,
            state_code          VARCHAR(3),
            city                TEXT,
            city_normalized     TEXT,
            postal_code         TEXT,
            metro_area          TEXT,
            latitude            DOUBLE PRECISION,
            longitude           DOUBLE PRECISION,
            raw_location_text   TEXT,
            created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT job_locations_lat_range
                CHECK (latitude IS NULL OR latitude BETWEEN -90 AND 90),
            CONSTRAINT job_locations_lon_range
                CHECK (longitude IS NULL OR longitude BETWEEN -180 AND 180)
        )
        """
    )
    # COALESCE keeps the uniqueness meaningful when parts are unknown: NULLs would
    # otherwise make every partial location distinct and explode the dimension.
    op.execute(
        """
        CREATE UNIQUE INDEX job_locations_uq ON job_locations (
            country_code,
            COALESCE(state_code, ''),
            COALESCE(city_normalized, ''),
            COALESCE(postal_code, '')
        )
        """
    )
    op.create_index(
        "job_locations_country_state_idx", "job_locations", ["country_code", "state_code"]
    )

    # -------------------------------------------------------------------- skills
    op.execute(
        """
        CREATE TABLE skills (
            id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            name            TEXT NOT NULL,
            slug            TEXT NOT NULL UNIQUE,
            category        TEXT,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX skills_name_trgm_idx ON skills USING GIN (name gin_trgm_ops)")

    op.execute(
        """
        CREATE TABLE job_categories (
            id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            name            TEXT NOT NULL,
            slug            TEXT NOT NULL UNIQUE,
            parent_id       BIGINT REFERENCES job_categories(id) ON DELETE SET NULL,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )

    # ----------------------------------------------------------------------- jobs
    op.execute(
        """
        CREATE TABLE jobs (
            id                    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,

            -- dedupe cluster key: several job_sources rows may resolve to one canonical job
            canonical_job_id      UUID        NOT NULL DEFAULT gen_random_uuid(),

            title                 TEXT        NOT NULL,
            title_normalized      TEXT        NOT NULL,
            description_text      TEXT,
            description_html      TEXT,

            company_id            BIGINT      REFERENCES companies(id) ON DELETE RESTRICT,
            location_id           BIGINT      REFERENCES job_locations(id) ON DELETE SET NULL,

            -- denormalized location: keeps the hot feed query single-table
            country_code          CHAR(2),
            state_code            VARCHAR(3),
            city                  TEXT,

            remote_type           remote_type      NOT NULL DEFAULT 'UNKNOWN',
            employment_type       employment_type  NOT NULL DEFAULT 'UNKNOWN',
            seniority             TEXT,
            department            TEXT,

            salary_min            NUMERIC(14, 2),
            salary_max            NUMERIC(14, 2),
            salary_currency       CHAR(3),
            salary_interval       salary_interval NOT NULL DEFAULT 'UNKNOWN',

            -- ---- the seven timestamps; see module docstring -------------------
            posted_at             TIMESTAMPTZ,   -- source truth. NULLable (19%), may be future
            posted_at_is_valid    BOOLEAN     NOT NULL DEFAULT FALSE,
            first_seen_at         TIMESTAMPTZ NOT NULL DEFAULT now(),  -- our clock, immutable
            last_seen_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
            last_updated_at       TIMESTAMPTZ,   -- only when content_hash actually changes
            source_fetched_at     TIMESTAMPTZ,   -- upstream pipeline fetch time
            close_at              TIMESTAMPTZ,   -- employer-stated expiry (expires_at)
            closed_at             TIMESTAMPTZ,   -- when the source detected closure

            status                job_status  NOT NULL DEFAULT 'ACTIVE',

            job_url               TEXT,
            apply_url             TEXT,
            apply_url_canonical   TEXT,
            apply_url_hash        BYTEA,        -- sha256 of canonical URL (dedupe L2)

            source                TEXT        NOT NULL,
            content_hash          BYTEA       NOT NULL,   -- change detection
            dedupe_fingerprint    BYTEA       NOT NULL,   -- dedupe L4

            search_vector         TSVECTOR,

            created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),

            CONSTRAINT jobs_salary_order
                CHECK (salary_min IS NULL OR salary_max IS NULL OR salary_min <= salary_max),
            CONSTRAINT jobs_salary_nonneg
                CHECK ((salary_min IS NULL OR salary_min >= 0)
                   AND (salary_max IS NULL OR salary_max >= 0)),
            CONSTRAINT jobs_country_code_shape
                CHECK (country_code IS NULL OR country_code ~ '^[A-Z]{2}$'),
            CONSTRAINT jobs_currency_shape
                CHECK (salary_currency IS NULL OR salary_currency ~ '^[A-Z]{3}$'),
            -- a valid posted_at must actually exist
            CONSTRAINT jobs_posted_valid_requires_value
                CHECK (NOT posted_at_is_valid OR posted_at IS NOT NULL),
            -- closure states must record when they happened
            CONSTRAINT jobs_closed_requires_timestamp
                CHECK (status <> 'EXPIRED' OR closed_at IS NOT NULL OR close_at IS NOT NULL)
        )
        """
    )

    # search_vector is maintained by trigger rather than a GENERATED column because
    # to_tsvector over a nullable multi-column concat is not IMMUTABLE-safe in a
    # generated expression across PG versions.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION jobs_update_search_vector()
        RETURNS TRIGGER AS $$
        BEGIN
            NEW.search_vector :=
                setweight(to_tsvector('english', coalesce(NEW.title, '')), 'A') ||
                setweight(to_tsvector('english', coalesce(NEW.department, '')), 'B') ||
                setweight(to_tsvector('english', coalesce(NEW.city, '')), 'C') ||
                setweight(to_tsvector('english', left(coalesce(NEW.description_text, ''), 100000)), 'D');
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER jobs_search_vector_trigger
        BEFORE INSERT OR UPDATE OF title, department, city, description_text ON jobs
        FOR EACH ROW EXECUTE FUNCTION jobs_update_search_vector()
        """
    )
    op.execute(
        "CREATE TRIGGER jobs_set_updated_at BEFORE UPDATE ON jobs "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )

    # ---- jobs indexes -------------------------------------------------------
    # Partial WHERE status='ACTIVE' roughly halves these: verified data is ~50% closed.
    op.execute(
        "CREATE INDEX jobs_feed_idx ON jobs (country_code, posted_at DESC, id DESC) "
        "WHERE status = 'ACTIVE' AND posted_at_is_valid"
    )
    op.execute(
        "CREATE INDEX jobs_state_idx ON jobs (country_code, state_code, posted_at DESC, id DESC) "
        "WHERE status = 'ACTIVE'"
    )
    op.execute(
        "CREATE INDEX jobs_remote_idx ON jobs (country_code, remote_type, posted_at DESC, id DESC) "
        "WHERE status = 'ACTIVE'"
    )
    op.execute(
        "CREATE INDEX jobs_company_active_idx ON jobs (company_id, posted_at DESC) "
        "WHERE status = 'ACTIVE'"
    )
    # "new to our platform today" -- detection feed, distinct from posted feed
    op.create_index("jobs_first_seen_idx", "jobs", [sa.text("first_seen_at DESC"), sa.text("id DESC")])
    op.create_index("jobs_last_seen_idx", "jobs", [sa.text("last_seen_at DESC")])
    op.create_index("jobs_last_updated_idx", "jobs", [sa.text("last_updated_at DESC")])
    op.create_index("jobs_created_at_idx", "jobs", [sa.text("created_at DESC")])
    op.create_index("jobs_status_idx", "jobs", ["status"])
    op.execute("CREATE INDEX jobs_search_vector_idx ON jobs USING GIN (search_vector)")
    op.execute("CREATE UNIQUE INDEX jobs_canonical_uq ON jobs (canonical_job_id)")
    op.execute(
        "CREATE INDEX jobs_apply_url_hash_idx ON jobs (apply_url_hash) "
        "WHERE apply_url_hash IS NOT NULL"
    )
    op.create_index("jobs_dedupe_fingerprint_idx", "jobs", ["dedupe_fingerprint"])
    op.execute(
        "CREATE INDEX jobs_title_trgm_idx ON jobs USING GIN (title_normalized gin_trgm_ops)"
    )

    # ------------------------------------------------------------- job_sources
    # Provenance. UNIQUE (source, external_job_id) is dedupe level 1 AND the idempotency
    # guarantee: reprocessing a file can never create a second job for the same source row.
    op.execute(
        """
        CREATE TABLE job_sources (
            id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            job_id              BIGINT      NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
            source              TEXT        NOT NULL,
            external_job_id     TEXT        NOT NULL,
            source_job_url      TEXT,
            source_apply_url    TEXT,
            ats_provider        TEXT,
            is_primary          BOOLEAN     NOT NULL DEFAULT TRUE,
            matched_by          dedupe_level NOT NULL DEFAULT 'L1_SOURCE_ID',
            first_seen_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
            last_seen_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
            source_payload_ref  TEXT,        -- object-storage key of the archived raw row
            CONSTRAINT job_sources_uq UNIQUE (source, external_job_id)
        )
        """
    )
    op.create_index("job_sources_job_id_idx", "job_sources", ["job_id"])
    op.create_index("job_sources_last_seen_idx", "job_sources", ["source", sa.text("last_seen_at DESC")])

    # --------------------------------------------------- job_skills / categories
    op.execute(
        """
        CREATE TABLE job_skills (
            job_id      BIGINT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
            skill_id    BIGINT NOT NULL REFERENCES skills(id) ON DELETE CASCADE,
            confidence  REAL,
            PRIMARY KEY (job_id, skill_id),
            CONSTRAINT job_skills_confidence_range
                CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1)
        )
        """
    )
    op.create_index("job_skills_skill_idx", "job_skills", ["skill_id"])

    op.execute(
        """
        CREATE TABLE job_category_map (
            job_id      BIGINT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
            category_id BIGINT NOT NULL REFERENCES job_categories(id) ON DELETE CASCADE,
            PRIMARY KEY (job_id, category_id)
        )
        """
    )
    op.create_index("job_category_map_category_idx", "job_category_map", ["category_id"])

    # -------------------------------------------- job_events (PARTITIONED by month)
    # Append-only history. PK includes occurred_at because Postgres requires the
    # partition key in every unique constraint.
    op.execute(
        """
        CREATE TABLE job_events (
            id              BIGINT GENERATED ALWAYS AS IDENTITY,
            job_id          BIGINT          NOT NULL,
            event_type      job_event_type  NOT NULL,
            occurred_at     TIMESTAMPTZ     NOT NULL DEFAULT now(),
            source          TEXT,
            sync_run_id     BIGINT,
            previous_status job_status,
            new_status      job_status,
            metadata        JSONB,
            PRIMARY KEY (id, occurred_at)
        ) PARTITION BY RANGE (occurred_at)
        """
    )
    op.create_index("job_events_job_id_idx", "job_events", ["job_id", sa.text("occurred_at DESC")])
    op.create_index("job_events_type_idx", "job_events", ["event_type", sa.text("occurred_at DESC")])
    op.create_index("job_events_sync_run_idx", "job_events", ["sync_run_id"])

    # ----------------------------------------- job_snapshots (PARTITIONED by month)
    # Point-in-time copies for "what did this job look like then" and diff analytics.
    op.execute(
        """
        CREATE TABLE job_snapshots (
            id              BIGINT GENERATED ALWAYS AS IDENTITY,
            job_id          BIGINT      NOT NULL,
            captured_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
            content_hash    BYTEA       NOT NULL,
            payload         JSONB       NOT NULL,
            sync_run_id     BIGINT,
            PRIMARY KEY (id, captured_at)
        ) PARTITION BY RANGE (captured_at)
        """
    )
    op.create_index(
        "job_snapshots_job_id_idx", "job_snapshots", ["job_id", sa.text("captured_at DESC")]
    )

    for year, month in _INITIAL_PARTITION_MONTHS:
        start, end = _month_bounds(year, month)
        suffix = f"{year:04d}_{month:02d}"
        op.execute(
            f"CREATE TABLE job_events_{suffix} PARTITION OF job_events "
            f"FOR VALUES FROM ('{start}') TO ('{end}')"
        )
        op.execute(
            f"CREATE TABLE job_snapshots_{suffix} PARTITION OF job_snapshots "
            f"FOR VALUES FROM ('{start}') TO ('{end}')"
        )

    # DEFAULT partitions catch anything outside the pre-created window so an insert can
    # never fail outright; a monitoring check alerts if they are ever non-empty.
    op.execute("CREATE TABLE job_events_default PARTITION OF job_events DEFAULT")
    op.execute("CREATE TABLE job_snapshots_default PARTITION OF job_snapshots DEFAULT")

    # ------------------------------------------------------------------ sync_runs
    op.execute(
        """
        CREATE TABLE sync_runs (
            id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            sync_uuid           UUID        NOT NULL DEFAULT gen_random_uuid(),
            source              TEXT        NOT NULL,
            trigger             sync_trigger NOT NULL DEFAULT 'SCHEDULED',
            status              sync_status NOT NULL DEFAULT 'PENDING',
            started_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
            finished_at         TIMESTAMPTZ,
            duration_seconds    DOUBLE PRECISION,
            files_discovered    INTEGER     NOT NULL DEFAULT 0,
            files_processed     INTEGER     NOT NULL DEFAULT 0,
            files_failed        INTEGER     NOT NULL DEFAULT 0,
            rows_processed      BIGINT      NOT NULL DEFAULT 0,
            rows_accepted       BIGINT      NOT NULL DEFAULT 0,
            rows_rejected       BIGINT      NOT NULL DEFAULT 0,
            rows_inserted       BIGINT      NOT NULL DEFAULT 0,
            rows_updated        BIGINT      NOT NULL DEFAULT 0,
            duplicates_found    BIGINT      NOT NULL DEFAULT 0,
            bytes_downloaded    BIGINT      NOT NULL DEFAULT 0,
            error_count         INTEGER     NOT NULL DEFAULT 0,
            worker_id           TEXT,
            config_snapshot     JSONB,
            CONSTRAINT sync_runs_uuid_uq UNIQUE (sync_uuid),
            CONSTRAINT sync_runs_counts_nonneg
                CHECK (rows_processed >= 0 AND rows_accepted >= 0 AND rows_rejected >= 0)
        )
        """
    )
    op.create_index("sync_runs_source_started_idx", "sync_runs", ["source", sa.text("started_at DESC")])
    op.create_index("sync_runs_status_idx", "sync_runs", ["status", sa.text("started_at DESC")])
    # At most one non-terminal run per source: prevents two schedulers double-ingesting.
    op.execute(
        "CREATE UNIQUE INDEX sync_runs_one_active_per_source ON sync_runs (source) "
        "WHERE status IN ('PENDING', 'RUNNING')"
    )

    # ------------------------------------------------------- sync_files (checkpoint)
    # The resumability unit. UNIQUE (source, remote_path, remote_etag) means a re-published
    # file (new etag/size) is treated as new work while a byte-identical one is skipped.
    op.execute(
        """
        CREATE TABLE sync_files (
            id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            sync_run_id         BIGINT      NOT NULL REFERENCES sync_runs(id) ON DELETE CASCADE,
            source              TEXT        NOT NULL,
            remote_path         TEXT        NOT NULL,
            remote_size_bytes   BIGINT,
            remote_etag         TEXT,
            file_date           DATE,
            archived_object_key TEXT,
            status              sync_status NOT NULL DEFAULT 'PENDING',
            row_groups_total    INTEGER,
            row_groups_done     INTEGER     NOT NULL DEFAULT 0,
            rows_committed      BIGINT      NOT NULL DEFAULT 0,
            started_at          TIMESTAMPTZ,
            finished_at         TIMESTAMPTZ,
            error_message       TEXT,
            CONSTRAINT sync_files_progress_sane
                CHECK (row_groups_total IS NULL OR row_groups_done <= row_groups_total)
        )
        """
    )
    op.create_index("sync_files_run_idx", "sync_files", ["sync_run_id"])
    op.create_index("sync_files_date_idx", "sync_files", ["source", sa.text("file_date DESC")])
    op.execute(
        "CREATE UNIQUE INDEX sync_files_completed_uq ON sync_files "
        "(source, remote_path, COALESCE(remote_etag, '')) WHERE status = 'SUCCEEDED'"
    )

    # ----------------------------------------------------------------- sync_errors
    # Every rejected row lands here with a reason. Nothing is silently discarded.
    op.execute(
        """
        CREATE TABLE sync_errors (
            id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            sync_run_id     BIGINT      NOT NULL REFERENCES sync_runs(id) ON DELETE CASCADE,
            sync_file_id    BIGINT      REFERENCES sync_files(id) ON DELETE CASCADE,
            source          TEXT        NOT NULL,
            external_job_id TEXT,
            reason          TEXT        NOT NULL,
            error_message   TEXT,
            row_payload     JSONB,
            occurred_at     TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.create_index("sync_errors_run_idx", "sync_errors", ["sync_run_id"])
    op.create_index("sync_errors_reason_idx", "sync_errors", ["reason", sa.text("occurred_at DESC")])

    # ---------------------------------------------------------------------- users
    op.execute(
        """
        CREATE TABLE users (
            id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            email               CITEXT      NOT NULL UNIQUE,
            password_hash       TEXT,
            full_name           TEXT,
            role                user_role   NOT NULL DEFAULT 'USER',
            is_active           BOOLEAN     NOT NULL DEFAULT TRUE,
            email_verified_at   TIMESTAMPTZ,
            last_login_at       TIMESTAMPTZ,
            failed_login_count  INTEGER     NOT NULL DEFAULT 0,
            locked_until        TIMESTAMPTZ,
            created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT users_email_shape CHECK (position('@' in email) > 1)
        )
        """
    )
    op.execute(
        "CREATE TRIGGER users_set_updated_at BEFORE UPDATE ON users "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )

    op.execute(
        """
        CREATE TABLE saved_jobs (
            id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            user_id     BIGINT      NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            job_id      BIGINT      NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
            notes       TEXT,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT saved_jobs_uq UNIQUE (user_id, job_id)
        )
        """
    )
    op.create_index("saved_jobs_user_idx", "saved_jobs", ["user_id", sa.text("created_at DESC")])

    op.execute(
        """
        CREATE TABLE saved_searches (
            id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            user_id     BIGINT      NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            name        TEXT        NOT NULL,
            query       JSONB       NOT NULL,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT saved_searches_uq UNIQUE (user_id, name)
        )
        """
    )
    op.execute(
        "CREATE TRIGGER saved_searches_set_updated_at BEFORE UPDATE ON saved_searches "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )

    op.execute(
        """
        CREATE TABLE user_alerts (
            id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            user_id             BIGINT          NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            saved_search_id     BIGINT          REFERENCES saved_searches(id) ON DELETE CASCADE,
            name                TEXT            NOT NULL,
            query               JSONB           NOT NULL,
            channel             alert_channel   NOT NULL DEFAULT 'EMAIL',
            frequency           alert_frequency NOT NULL DEFAULT 'DAILY',
            is_active           BOOLEAN         NOT NULL DEFAULT TRUE,
            last_run_at         TIMESTAMPTZ,
            last_job_seen_at    TIMESTAMPTZ,
            created_at          TIMESTAMPTZ     NOT NULL DEFAULT now(),
            updated_at          TIMESTAMPTZ     NOT NULL DEFAULT now()
        )
        """
    )
    op.create_index(
        "user_alerts_due_idx", "user_alerts", ["frequency", "last_run_at"],
        postgresql_where=sa.text("is_active"),
    )
    op.execute(
        "CREATE TRIGGER user_alerts_set_updated_at BEFORE UPDATE ON user_alerts "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )

    # Outbox. UNIQUE (alert_id, job_id) is what makes "never send a duplicate alert for
    # the same job" a database guarantee rather than application-level hope.
    op.execute(
        """
        CREATE TABLE alert_deliveries (
            id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            alert_id        BIGINT      NOT NULL REFERENCES user_alerts(id) ON DELETE CASCADE,
            job_id          BIGINT      NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
            channel         alert_channel NOT NULL,
            status          TEXT        NOT NULL DEFAULT 'PENDING',
            scheduled_for   TIMESTAMPTZ NOT NULL DEFAULT now(),
            sent_at         TIMESTAMPTZ,
            attempts        INTEGER     NOT NULL DEFAULT 0,
            last_error      TEXT,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT alert_deliveries_uq UNIQUE (alert_id, job_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX alert_deliveries_pending_idx ON alert_deliveries "
        "(scheduled_for) WHERE status = 'PENDING'"
    )

    # ------------------------------------------------------------------ audit_log
    op.execute(
        """
        CREATE TABLE audit_log (
            id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            user_id         BIGINT      REFERENCES users(id) ON DELETE SET NULL,
            action          TEXT        NOT NULL,
            resource_type   TEXT,
            resource_id     TEXT,
            ip_address      INET,
            user_agent      TEXT,
            metadata        JSONB,
            occurred_at     TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.create_index("audit_log_user_idx", "audit_log", ["user_id", sa.text("occurred_at DESC")])
    op.create_index("audit_log_action_idx", "audit_log", ["action", sa.text("occurred_at DESC")])


def downgrade() -> None:
    # Order matters: dependents first, then partitioned parents (partitions drop with
    # the parent), then enums, then functions.
    for table in (
        "audit_log",
        "alert_deliveries",
        "user_alerts",
        "saved_searches",
        "saved_jobs",
        "users",
        "sync_errors",
        "sync_files",
        "sync_runs",
        "job_snapshots",
        "job_events",
        "job_category_map",
        "job_skills",
        "job_sources",
        "jobs",
        "job_categories",
        "skills",
        "job_locations",
        "company_sources",
        "companies",
    ):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")

    op.execute("DROP FUNCTION IF EXISTS jobs_update_search_vector() CASCADE")
    op.execute("DROP FUNCTION IF EXISTS set_updated_at() CASCADE")

    for enum_name in (
        "dedupe_level",
        "alert_channel",
        "alert_frequency",
        "user_role",
        "sync_trigger",
        "sync_status",
        "job_event_type",
        "salary_interval",
        "employment_type",
        "remote_type",
        "job_status",
    ):
        op.execute(f"DROP TYPE IF EXISTS {enum_name}")
