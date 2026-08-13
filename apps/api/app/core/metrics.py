"""Prometheus metric definitions.

Declared centrally so the API and the workers agree on names and labels. The ingestion
counters are defined here in Milestone 1 even though nothing increments them yet: the
admin dashboard and the alerting rules in the spec are written against these exact names,
and defining them late would mean rewriting dashboards.

Label cardinality is kept deliberately low. A label like ``company_id`` or ``job_id``
would create millions of series and take the metrics backend down.
"""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest
from prometheus_client.exposition import CONTENT_TYPE_LATEST

#: A dedicated registry rather than the global default keeps test runs isolated.
REGISTRY = CollectorRegistry()

# ---- HTTP ------------------------------------------------------------------

http_requests_total = Counter(
    "jobplatform_http_requests_total",
    "Total HTTP requests.",
    labelnames=("method", "endpoint", "status_class"),
    registry=REGISTRY,
)

http_request_duration_seconds = Histogram(
    "jobplatform_http_request_duration_seconds",
    "HTTP request latency.",
    labelnames=("method", "endpoint"),
    # Buckets straddle the 300 ms p95 target from the spec so the SLO is directly readable.
    buckets=(0.01, 0.025, 0.05, 0.1, 0.2, 0.3, 0.5, 1.0, 2.5, 5.0, 10.0),
    registry=REGISTRY,
)

# ---- Ingestion -------------------------------------------------------------

ingestion_runs_total = Counter(
    "jobplatform_ingestion_runs_total",
    "Ingestion runs by terminal status.",
    labelnames=("source", "status"),
    registry=REGISTRY,
)

ingestion_rows_total = Counter(
    "jobplatform_ingestion_rows_total",
    "Source rows by outcome.",
    labelnames=("source", "outcome"),  # processed | accepted | rejected | duplicate
    registry=REGISTRY,
)

ingestion_duration_seconds = Histogram(
    "jobplatform_ingestion_duration_seconds",
    "Wall-clock duration of an ingestion run.",
    labelnames=("source",),
    buckets=(10, 30, 60, 300, 600, 1800, 3600, 7200),
    registry=REGISTRY,
)

ingestion_last_success_timestamp = Gauge(
    "jobplatform_ingestion_last_success_timestamp_seconds",
    "Unix time of the last successful ingestion. Alert when this goes stale.",
    labelnames=("source",),
    registry=REGISTRY,
)

ingestion_source_lag_seconds = Gauge(
    "jobplatform_ingestion_source_lag_seconds",
    "Age of the newest file available at the source. Detects upstream publication gaps.",
    labelnames=("source",),
    registry=REGISTRY,
)

ingestion_rejections_total = Counter(
    "jobplatform_ingestion_rejections_total",
    "Rejected rows by reason. Drives the data-quality dashboard.",
    labelnames=("source", "reason"),
    registry=REGISTRY,
)

# ---- Jobs inventory --------------------------------------------------------

jobs_total = Gauge(
    "jobplatform_jobs_total",
    "Jobs currently stored, by status and country.",
    labelnames=("status", "country_code"),
    registry=REGISTRY,
)

jobs_posted_recently = Gauge(
    "jobplatform_jobs_posted_recently",
    "Jobs with a valid posted_at inside a rolling window.",
    labelnames=("window",),  # last_hour | last_6_hours | today
    registry=REGISTRY,
)


def render_metrics() -> tuple[bytes, str]:
    """Serialise the registry for the /metrics endpoint."""
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST
