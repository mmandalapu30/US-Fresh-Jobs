"""API operational endpoint tests."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture
def client(minimal_env: None) -> TestClient:
    return TestClient(create_app(), raise_server_exceptions=False)


class TestHealth:
    def test_health_is_ok(self, client: TestClient) -> None:
        response = client.get("/health")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["service"] == "api"
        assert body["uptime_seconds"] >= 0

    def test_health_does_not_touch_dependencies(self, client: TestClient) -> None:
        """Liveness must stay green when Postgres/Redis are down, otherwise a database
        blip triggers a restart loop that makes the outage worse. The fixture points at
        a database that is not running, so a dependency check here would fail."""
        assert client.get("/health").status_code == 200

    def test_request_id_is_echoed(self, client: TestClient) -> None:
        response = client.get("/health", headers={"X-Request-ID": "abc-123"})
        assert response.headers["X-Request-ID"] == "abc-123"

    def test_request_id_is_generated_when_absent(self, client: TestClient) -> None:
        assert client.get("/health").headers.get("X-Request-ID")


class TestReady:
    def test_reports_503_when_dependencies_are_down(self, client: TestClient) -> None:
        """The fixture environment points at a database and Redis that do not exist."""
        response = client.get("/ready")
        assert response.status_code == 503
        body = response.json()
        assert body["status"] == "not_ready"
        assert {d["name"] for d in body["dependencies"]} == {"postgres", "redis"}

    def test_dependency_errors_do_not_leak_credentials(self, client: TestClient) -> None:
        """An exception string can embed a DSN with a password; only the type is reported.

        Asserted against the actual credential values rather than the word "password",
        because legitimate exception type names such as ``InvalidPasswordError`` contain
        that substring without leaking anything.
        """
        body = client.get("/ready").json()
        serialized = str(body)
        for secret in ("testpass", "testuser", "127.0.0.1:1", "://"):
            assert secret not in serialized, f"{secret!r} leaked into the readiness payload"
        for dep in body["dependencies"]:
            if dep["error"]:
                # Bare exception class name, e.g. "ConnectionRefusedError".
                assert dep["error"].isidentifier()


class TestSecurityHeaders:
    @pytest.mark.parametrize(
        ("header", "expected"),
        [
            ("X-Content-Type-Options", "nosniff"),
            ("X-Frame-Options", "DENY"),
            ("Referrer-Policy", "strict-origin-when-cross-origin"),
        ],
    )
    def test_present(self, client: TestClient, header: str, expected: str) -> None:
        assert client.get("/health").headers[header] == expected

    def test_hsts_absent_outside_production(self, client: TestClient) -> None:
        assert "Strict-Transport-Security" not in client.get("/health").headers


class TestMetrics:
    def test_exposes_prometheus_format(self, client: TestClient) -> None:
        response = client.get("/metrics")
        assert response.status_code == 200
        assert "text/plain" in response.headers["content-type"]

    def test_ingestion_metrics_are_predeclared(self, client: TestClient) -> None:
        """Dashboards and alert rules are written against these names, so they must
        exist before the ingestion worker does."""
        body = client.get("/metrics").text
        for metric in (
            "jobplatform_ingestion_rows_total",
            "jobplatform_ingestion_last_success_timestamp_seconds",
            "jobplatform_ingestion_source_lag_seconds",
            "jobplatform_jobs_total",
        ):
            assert metric in body, f"{metric} is not exposed"


class TestMetaEndpoints:
    def test_states_returns_fifty_one(self, client: TestClient) -> None:
        response = client.get("/api/v1/locations/states")
        assert response.status_code == 200
        states = response.json()
        assert len(states) == 51
        assert {"code": "MI", "name": "Michigan", "is_territory": False} in states

    def test_states_can_include_territories(self, client: TestClient) -> None:
        response = client.get("/api/v1/locations/states?include_territories=true")
        assert len(response.json()) == 56

    def test_states_are_cacheable(self, client: TestClient) -> None:
        assert "max-age" in client.get("/api/v1/locations/states").headers["Cache-Control"]

    def test_enums_are_exposed(self, client: TestClient) -> None:
        body = client.get("/api/v1/meta/enums").json()
        assert "ACTIVE" in body["job_status"]
        assert "REMOTE" in body["remote_type"]
        assert "NEW_LAST_HOUR" in body["freshness_bucket"]


class TestErrorContract:
    def test_404_uses_the_error_envelope(self, client: TestClient) -> None:
        body = client.get("/api/v1/does-not-exist").json()
        assert body["error"]["code"] == "not_found"
        assert body["error"]["request_id"]

    def test_validation_error_reports_fields_not_values(self, client: TestClient) -> None:
        """Echoing submitted values back could reflect a secret into logs or responses."""
        response = client.get("/api/v1/locations/states?include_territories=not-a-bool")
        assert response.status_code == 422
        body = response.json()
        assert body["error"]["code"] == "validation_error"
        assert "not-a-bool" not in str(body)
