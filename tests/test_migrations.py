"""Database schema tests.

These assert the guarantees the product depends on, executed against a real PostgreSQL:

* the seven-timestamp model exists and ``posted_at`` is nullable (19% of source rows)
* dedupe level 1 is a database UNIQUE, which is what makes ingestion idempotent
* alert delivery cannot duplicate for the same (alert, job)
* partitioned history routes correctly and never rejects an out-of-range insert
* the search vector is maintained automatically
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


def _fetch(conn: object, sql: str, params: tuple = ()) -> list[tuple]:
    with conn.cursor() as cur:  # type: ignore[attr-defined]
        cur.execute(sql, params)
        return cur.fetchall()


class TestSchemaShape:
    def test_all_expected_tables_exist(self, db_connection: object) -> None:
        rows = _fetch(
            db_connection,
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'",
        )
        tables = {r[0] for r in rows}
        required = {
            "users",
            "companies",
            "jobs",
            "job_sources",
            "job_locations",
            "job_events",
            "job_snapshots",
            "job_skills",
            "job_categories",
            "saved_jobs",
            "saved_searches",
            "user_alerts",
            "sync_runs",
            "sync_errors",
        }
        assert required <= tables, f"missing: {sorted(required - tables)}"

    def test_seven_timestamp_model(self, db_connection: object) -> None:
        """Each timestamp carries distinct information; collapsing any two loses data."""
        rows = _fetch(
            db_connection,
            "SELECT column_name, is_nullable, data_type FROM information_schema.columns "
            "WHERE table_name = 'jobs'",
        )
        cols = {r[0]: (r[1], r[2]) for r in rows}
        for name in (
            "posted_at",
            "first_seen_at",
            "last_seen_at",
            "last_updated_at",
            "source_fetched_at",
            "close_at",
            "closed_at",
        ):
            assert name in cols, f"jobs.{name} is missing"
            assert cols[name][1] == "timestamp with time zone", f"{name} must be timestamptz"

    def test_posted_at_is_nullable(self, db_connection: object) -> None:
        """19.3% of verified source rows have no posted_at. A NOT NULL here would
        force fabricating a timestamp, which the spec forbids."""
        rows = _fetch(
            db_connection,
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_name = 'jobs' AND column_name = 'posted_at'",
        )
        assert rows[0][0] == "YES"

    def test_first_seen_at_is_not_nullable(self, db_connection: object) -> None:
        """Our own clock always knows when we first saw a row."""
        rows = _fetch(
            db_connection,
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_name = 'jobs' AND column_name = 'first_seen_at'",
        )
        assert rows[0][0] == "NO"


class TestDeduplicationGuarantees:
    def test_source_external_id_is_unique(self, db_connection: object) -> None:
        """Dedupe level 1 AND the idempotency guarantee: reprocessing a file cannot
        create a second job for the same source row."""
        import psycopg

        with db_connection.cursor() as cur:  # type: ignore[attr-defined]
            cur.execute(
                "INSERT INTO jobs (title, title_normalized, source, content_hash, "
                "dedupe_fingerprint) VALUES ('t','t','source-a','\\x01','\\xaa') "
                "RETURNING id"
            )
            job_id = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO job_sources (job_id, source, external_job_id) "
                "VALUES (%s, 'source-a', 'jobvite:acme/x1')",
                (job_id,),
            )
            with pytest.raises(psycopg.errors.UniqueViolation):
                cur.execute(
                    "INSERT INTO job_sources (job_id, source, external_job_id) "
                    "VALUES (%s, 'source-a', 'jobvite:acme/x1')",
                    (job_id,),
                )

    def test_same_job_from_two_sources_is_allowed(self, db_connection: object) -> None:
        """Spec test case 2. Provenance from both sources is preserved on one job.

        Source names are deliberately neutral: the schema must not know or care which
        provider a row came from.
        """
        with db_connection.cursor() as cur:  # type: ignore[attr-defined]
            cur.execute(
                "INSERT INTO jobs (title, title_normalized, source, content_hash, "
                "dedupe_fingerprint) VALUES ('t','t','source-a','\\x01','\\xaa') "
                "RETURNING id"
            )
            job_id = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO job_sources (job_id, source, external_job_id, is_primary) "
                "VALUES (%s,'source-a','a-1',TRUE), (%s,'source-b','b-1',FALSE)",
                (job_id, job_id),
            )
            cur.execute("SELECT count(*) FROM job_sources WHERE job_id = %s", (job_id,))
            assert cur.fetchone()[0] == 2


class TestDataQualityConstraints:
    @pytest.mark.parametrize(
        ("columns", "values", "constraint"),
        [
            ("salary_min, salary_max", "200000, 100000", "jobs_salary_order"),
            ("posted_at, posted_at_is_valid", "NULL, TRUE", "jobs_posted_valid_requires_value"),
            ("country_code", "'us'", "jobs_country_code_shape"),
            ("salary_currency", "'usd'", "jobs_currency_shape"),
            ("status", "'EXPIRED'", "jobs_closed_requires_timestamp"),
        ],
    )
    def test_check_constraint_rejects(
        self, db_connection: object, columns: str, values: str, constraint: str
    ) -> None:
        import psycopg

        with db_connection.cursor() as cur:  # type: ignore[attr-defined]
            with pytest.raises(psycopg.errors.CheckViolation) as exc:
                # S608: `columns` and `values` come from the closed @parametrize list
                # above, never from input. Interpolation is unavoidable here because the
                # test varies the column list itself, which cannot be a bound parameter.
                cur.execute(
                    f"INSERT INTO jobs (title, title_normalized, source, content_hash, "  # noqa: S608
                    f"dedupe_fingerprint, {columns}) "
                    f"VALUES ('t','t','s','\\x01','\\xaa', {values})"
                )
            assert constraint in str(exc.value)

    def test_valid_job_inserts(self, db_connection: object) -> None:
        with db_connection.cursor() as cur:  # type: ignore[attr-defined]
            cur.execute(
                "INSERT INTO jobs (title, title_normalized, country_code, state_code, city, "
                "remote_type, employment_type, salary_min, salary_max, salary_currency, "
                "salary_interval, posted_at, posted_at_is_valid, source, content_hash, "
                "dedupe_fingerprint) VALUES "
                "('Senior Software Engineer','senior software engineer','US','MI','Detroit',"
                "'REMOTE','FULL_TIME',140000,170000,'USD','ANNUAL',now(),TRUE,"
                "'source-a','\\x01','\\xaa') RETURNING id"
            )
            assert cur.fetchone()[0] > 0


class TestSearchVector:
    def test_populated_on_insert(self, db_connection: object) -> None:
        with db_connection.cursor() as cur:  # type: ignore[attr-defined]
            cur.execute(
                "INSERT INTO jobs (title, title_normalized, description_text, source, "
                "content_hash, dedupe_fingerprint) VALUES "
                "('Senior Python Engineer','senior python engineer','We need Django skills',"
                "'s','\\x01','\\xaa') RETURNING id"
            )
            job_id = cur.fetchone()[0]
            cur.execute(
                "SELECT search_vector @@ to_tsquery('english','python') "
                "AND search_vector @@ to_tsquery('english','django') FROM jobs WHERE id=%s",
                (job_id,),
            )
            assert cur.fetchone()[0] is True

    def test_refreshed_on_title_update(self, db_connection: object) -> None:
        with db_connection.cursor() as cur:  # type: ignore[attr-defined]
            cur.execute(
                "INSERT INTO jobs (title, title_normalized, source, content_hash, "
                "dedupe_fingerprint) VALUES ('Cook','cook','s','\\x01','\\xaa') RETURNING id"
            )
            job_id = cur.fetchone()[0]
            cur.execute("UPDATE jobs SET title='Astronaut' WHERE id=%s", (job_id,))
            cur.execute(
                "SELECT search_vector @@ to_tsquery('english','astronaut') FROM jobs WHERE id=%s",
                (job_id,),
            )
            assert cur.fetchone()[0] is True


class TestPartitioning:
    def test_events_route_to_month_partitions(self, db_connection: object) -> None:
        with db_connection.cursor() as cur:  # type: ignore[attr-defined]
            cur.execute(
                "INSERT INTO jobs (title,title_normalized,source,content_hash,"
                "dedupe_fingerprint) VALUES ('t','t','s','\\x01','\\xaa') RETURNING id"
            )
            job_id = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO job_events (job_id,event_type,occurred_at) "
                "VALUES (%s,'CREATED','2026-08-15T10:00:00Z')",
                (job_id,),
            )
            cur.execute("SELECT count(*) FROM job_events_2026_08 WHERE job_id=%s", (job_id,))
            assert cur.fetchone()[0] == 1

    def test_out_of_range_lands_in_default_not_error(self, db_connection: object) -> None:
        """A missing partition must never fail an insert and lose history."""
        with db_connection.cursor() as cur:  # type: ignore[attr-defined]
            cur.execute(
                "INSERT INTO jobs (title,title_normalized,source,content_hash,"
                "dedupe_fingerprint) VALUES ('t','t','s','\\x01','\\xaa') RETURNING id"
            )
            job_id = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO job_events (job_id,event_type,occurred_at) "
                "VALUES (%s,'UPDATED','2019-01-01T00:00:00Z')",
                (job_id,),
            )
            cur.execute("SELECT count(*) FROM job_events_default WHERE job_id=%s", (job_id,))
            assert cur.fetchone()[0] == 1


class TestOperationalConstraints:
    def test_only_one_active_sync_run_per_source(self, db_connection: object) -> None:
        """Stops two schedulers double-ingesting the same file."""
        import psycopg

        with db_connection.cursor() as cur:  # type: ignore[attr-defined]
            cur.execute("INSERT INTO sync_runs (source,status) VALUES ('source-a','RUNNING')")
            with pytest.raises(psycopg.errors.UniqueViolation):
                cur.execute("INSERT INTO sync_runs (source,status) VALUES ('source-a','PENDING')")

    def test_finished_run_allows_a_new_one(self, db_connection: object) -> None:
        with db_connection.cursor() as cur:  # type: ignore[attr-defined]
            cur.execute("INSERT INTO sync_runs (source,status) VALUES ('source-a','RUNNING')")
            cur.execute("UPDATE sync_runs SET status='SUCCEEDED' WHERE source='source-a'")
            cur.execute("INSERT INTO sync_runs (source,status) VALUES ('source-a','RUNNING')")
            cur.execute("SELECT count(*) FROM sync_runs WHERE source='source-a'")
            assert cur.fetchone()[0] == 2

    def test_no_duplicate_alert_for_same_job(self, db_connection: object) -> None:
        """Spec: 'Do not send duplicate alerts for the same job' — enforced by the DB."""
        import psycopg

        with db_connection.cursor() as cur:  # type: ignore[attr-defined]
            cur.execute(
                "INSERT INTO jobs (title,title_normalized,source,content_hash,"
                "dedupe_fingerprint) VALUES ('t','t','s','\\x01','\\xaa') RETURNING id"
            )
            job_id = cur.fetchone()[0]
            cur.execute("INSERT INTO users (email) VALUES ('u@example.com') RETURNING id")
            user_id = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO user_alerts (user_id,name,query) VALUES (%s,'a','{}') RETURNING id",
                (user_id,),
            )
            alert_id = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO alert_deliveries (alert_id,job_id,channel) VALUES (%s,%s,'EMAIL')",
                (alert_id, job_id),
            )
            with pytest.raises(psycopg.errors.UniqueViolation):
                cur.execute(
                    "INSERT INTO alert_deliveries (alert_id,job_id,channel) VALUES (%s,%s,'EMAIL')",
                    (alert_id, job_id),
                )

    def test_email_uniqueness_is_case_insensitive(self, db_connection: object) -> None:
        """citext prevents duplicate accounts differing only by capitalisation."""
        import psycopg

        with db_connection.cursor() as cur:  # type: ignore[attr-defined]
            cur.execute("INSERT INTO users (email) VALUES ('User@Example.com')")
            with pytest.raises(psycopg.errors.UniqueViolation):
                cur.execute("INSERT INTO users (email) VALUES ('user@example.com')")


class TestIndexes:
    def test_hot_feed_index_exists_and_is_partial(self, db_connection: object) -> None:
        """The partial predicate halves the index; verified data is ~50% closed."""
        rows = _fetch(
            db_connection,
            "SELECT indexdef FROM pg_indexes WHERE indexname = 'jobs_feed_idx'",
        )
        assert rows, "jobs_feed_idx is missing"
        definition = rows[0][0]
        assert "WHERE" in definition
        assert "posted_at_is_valid" in definition

    def test_gin_index_on_search_vector(self, db_connection: object) -> None:
        rows = _fetch(
            db_connection,
            "SELECT indexdef FROM pg_indexes WHERE indexname = 'jobs_search_vector_idx'",
        )
        assert rows and "gin" in rows[0][0].lower()
