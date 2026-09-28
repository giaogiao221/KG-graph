# syntax=docker/dockerfile:1
# Backend image for the extraction review platform.
# Build context is the repository root: docker build -f backend/Dockerfile .

# ============================================================================
# Engine artifact stage
#
# The extraction engine is NOT vendored into this repository. It is shipped
# separately as a pre-built "engine artifact" (a tarball of the compiled
# engine). This stage receives that artifact via a build secret or a local
# file path and unpacks it into /engine.
#
# To build without an engine artifact (e.g. during CI or open-source review),
# pass ENGINE_ARTIFACT_PATH pointing at an empty directory or a dummy file.
# The engine layer will then contain only ENGINE_VERSION.md.
#
# To build the real artifact, run tools/build-engine-artifact.sh first, then:
#   docker build --build-arg ENGINE_ARTIFACT_PATH=engine-artifact.tar.gz \
#                -f backend/Dockerfile .
# ============================================================================
FROM python:3.12-slim AS engine-artifact
ARG ENGINE_ARTIFACT_PATH
WORKDIR /engine
COPY "${ENGINE_ARTIFACT_PATH}" ./
# If the artifact is a tarball, unpack it; if it's a directory (or missing),
# leave it alone. The unpack must be idempotent because the layer is cached.
RUN if [ -f "${ENGINE_ARTIFACT_PATH}" ] && [ "${ENGINE_ARTIFACT_PATH##*.}" = "gz" ]; then \
      tar -xzf "${ENGINE_ARTIFACT_PATH}" -C . && rm -f "${ENGINE_ARTIFACT_PATH}"; \
    fi
# Verify the engine is complete: config must exist, and either source or
# compiled bytecode must exist. This catches accidental empty artifacts.
RUN test -f src/config/pipeline_stages_v2.yaml \
    && (test -f src/book_engine/cli.py || test -f src/book_engine/cli.pyc)

# ============================================================================
# Engine compile stage
#
# The artifact already carries bytecode, so compileall is a safety net (it
# keeps this stage correct even if someone supplies a source-only artifact).
# Sources under src/ and scripts/ are stripped; tests/ keeps its plain .py files
# because it carries no algorithm and is reproducibility evidence.
# ============================================================================
FROM python:3.12-slim AS engine-compile
WORKDIR /engine
COPY --from=engine-artifact /engine ./
RUN python -m compileall -b -q src scripts start.py 2>/dev/null || true \
    && find . -name '*.py' -not -path './tests/*' -delete \
    && find . -type d -name __pycache__ -prune -exec rm -rf {} + \
    && test -f src/book_engine/cli.pyc

# ============================================================================
# Runtime stage
# ============================================================================
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
    PIP_EXTRA_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ \
    PIP_TRUSTED_HOST="pypi.tuna.tsinghua.edu.cn mirrors.aliyun.com"

WORKDIR /app

# Dependency layer: resolve declared dependencies before copying sources so
# code changes reuse this cached layer. The stub package keeps `pip install .`
# valid before the real sources arrive.
COPY backend/pyproject.toml ./
RUN mkdir app && touch app/__init__.py \
    && pip install --no-cache-dir ".[dev]" \
    && rm -rf app

# Application layer: runtime code, migrations, and the test suite used by the
# in-container acceptance checks (see docs/operations.md).
COPY backend/app ./app
COPY backend/alembic ./alembic
COPY backend/alembic.ini ./
COPY backend/tests ./tests
RUN pip install --no-cache-dir ".[dev]" \
    && groupadd --system --gid 10001 extraction \
    && useradd --system --uid 10001 --gid extraction --home-dir /app extraction \
    && mkdir -p /data/storage /data/work \
    && chown -R extraction:extraction /data

# Engine ships compiled inside the image at /legacy-engine.
COPY --from=engine-compile --chown=extraction:extraction /engine /legacy-engine

USER extraction
EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
