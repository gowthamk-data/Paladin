# syntax=docker/dockerfile:1.7
#
# Two-stage build:
#   * builder  - has compilers and installs every pinned wheel into a self-contained virtualenv
#   * runtime  - a clean slim image plus that virtualenv; no compiler toolchain is shipped
#
# Before this change the single-stage image also carried the full CUDA build of PyTorch
# (torch==2.14.0+cu130) plus its nvidia-* runtime dependencies, none of which are usable here:
# the gateway only ever runs a 384-dimension MiniLM sentence encoder on CPU.

# ---------------------------------------------------------------------------------------------
# Stage 1: build the virtualenv
# ---------------------------------------------------------------------------------------------
FROM python:3.11.16-slim-bookworm AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    VENV_PATH=/opt/venv

# Only needed if a dependency has no prebuilt wheel for this platform. It stays in this stage.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        python3-dev \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv "${VENV_PATH}"
ENV PATH="${VENV_PATH}/bin:${PATH}"

COPY gateway/requirements.txt /tmp/requirements.txt
# Byte-code caches are wasted bytes in a layer: they are regenerated on first import and can go
# stale after an upgrade. torch/include is only needed when compiling C++ extensions.
RUN pip install --upgrade pip setuptools wheel \
    && pip install -r /tmp/requirements.txt \
    && rm -f /tmp/requirements.txt \
    && find "${VENV_PATH}" -type d -name __pycache__ -prune -exec rm -rf {} + \
    && rm -rf "${VENV_PATH}/lib/python3.11/site-packages/torch/include"

# ---------------------------------------------------------------------------------------------
# Stage 2: runtime
# ---------------------------------------------------------------------------------------------
FROM python:3.11.16-slim-bookworm AS runtime

# libgomp1 is the OpenMP runtime the manylinux PyTorch CPU wheels link against; without it the
# import of torch fails. passwd provides useradd for the unprivileged service account.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgomp1 \
        passwd \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PATH="/opt/venv/bin:${PATH}" \
    # sentence-transformers reads this to pick its cache directory; docker-compose mounts a
    # named volume here so the embedding model is downloaded once, not on every container
    # recreation.
    SENTENCE_TRANSFORMERS_HOME=/app/.cache/sentence_transformers

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app

# The isolated-ABAC test harness (scripts/) lives next to the tests in their own
# directory; it is not part of the gateway app but must be importable inside the
# disposable test container.
COPY scripts/ ./scripts/

COPY gateway/ ./gateway/

# data/ is kept because ingestion (data/ingest.py) is run from inside this container with
# `docker compose exec -T -e PYTHONPATH=/app paladin-api python data/ingest.py`
# (PYTHONPATH is required: the image does not put /app on sys.path); it is a few kilobytes.
COPY data/ ./data/

# The RSA signing key pair is mounted read-only at runtime (see docker-compose.yml). The paths
# are created here so the process starts and fails closed on a missing mount rather than
# crashing with an unclear error.
RUN mkdir -p /app/keys /app/.cache/sentence_transformers \
    && useradd --create-home --uid 10001 --shell /usr/sbin/nologin paladin \
    && chown -R paladin:paladin /app

USER paladin

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)" || exit 1

CMD ["uvicorn", "gateway.main:app", "--host", "0.0.0.0", "--port", "8000"]
