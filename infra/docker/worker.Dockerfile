FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY packages/shared      ./packages/shared
COPY packages/schemas     ./packages/schemas
COPY workers/ingestion    ./workers/ingestion

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN pip install --upgrade pip \
 && pip install ./packages/shared ./packages/schemas \
 && pip install ./workers/ingestion


FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    # Parquet reads are memory-hungry; cap Arrow's thread pool so a worker cannot
    # starve the host. Tuned against the verified ~120 MB/day projected read.
    OMP_NUM_THREADS=2

RUN useradd --create-home --uid 10001 appuser

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY workers/ingestion/ingestion ./ingestion
COPY database ./database
# The operational entry points. This image is what scripts/daily.sh drives, and it invokes
# scripts/have_todays_file.py, scripts/ingest.py and scripts/enforce_retention.py by path --
# so without these the compose `command` fails on a missing file, not on anything subtle.
# They insert <root>/packages/* onto sys.path, which does not exist here and does not need
# to: the builder pip-installs those packages into /opt/venv.
COPY scripts ./scripts

USER appuser

CMD ["celery", "-A", "ingestion.celery_app", "worker", "--loglevel=INFO", "--concurrency=2"]
