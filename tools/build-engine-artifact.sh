#!/usr/bin/env bash
# Build the extraction engine artifact consumed by backend/Dockerfile.
#
# The engine source is not vendored into this repository. This script takes a
# source tree (KGchouqu_hzy layout: src/book_engine + src/config), compiles the
# Python sources to same-directory .pyc files, strips the .py sources, and packs
# the result into a tarball. The tarball is the only engine input the image
# build accepts, so a distributed image never contains readable engine code.
#
# Usage:
#   tools/build-engine-artifact.sh <engine-source-dir> [output-tarball]
#
# Example:
#   tools/build-engine-artifact.sh "$HOME/kgchouqu-hzy" engine-artifact.tar.gz
#
# Requirements:
#   - Docker (preferred) so the bytecode is produced by the same Python minor
#     version as the runtime image. Without Docker the local python is used and
#     the caller must ensure it matches the image's minor version.
#
# The produced tarball is git-ignored. Distribute it through your artifact
# store; do NOT commit it, and do NOT commit the engine sources.

set -euo pipefail

SOURCE_DIR="${1:?usage: build-engine-artifact.sh <engine-source-dir> [output-tarball]}"
OUTPUT="${2:-engine-artifact.tar.gz}"
PYTHON_IMAGE="${ENGINE_PYTHON_IMAGE:-python:3.12-slim}"

if [ ! -d "$SOURCE_DIR/src/book_engine" ]; then
    echo "error: $SOURCE_DIR does not look like an engine tree (no src/book_engine)" >&2
    exit 1
fi
if [ ! -f "$SOURCE_DIR/src/config/pipeline_stages_v2.yaml" ]; then
    echo "error: $SOURCE_DIR has no src/config/pipeline_stages_v2.yaml" >&2
    exit 1
fi

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT
STAGE="$WORKDIR/engine"

# Copy the engine, excluding VCS, virtualenvs, caches, and stale bytecode. Keep
# config, non-Python assets, tests, and docs so the artifact is self-describing.
echo "[1/3] staging engine sources from $SOURCE_DIR"
mkdir -p "$STAGE"
(
    cd "$SOURCE_DIR"
    find . -type d \( -name .git -o -name .venv -o -name __pycache__ \) -prune -o \
         -type f ! -name '*.pyc' -print0 \
    | while IFS= read -r -d '' f; do
        mkdir -p "$STAGE/$(dirname "$f")"
        cp -p "$f" "$STAGE/$f"
      done
)

# Compile .py -> same-directory .pyc and drop the sources. compileall -b keeps
# the .pyc next to the module path, which the engine's
# `Path(__file__).parents[2] / "config"` lookups rely on.
echo "[2/3] compiling sources to bytecode and stripping .py"
if command -v docker >/dev/null 2>&1; then
    # Windows bind-mounts into docker are unreliable from Git Bash, so feed the
    # staged tree through stdin via `docker run -i` and `tar`.
    CONTAINER=$(docker create "$PYTHON_IMAGE" sh -c '
        cd /engine
        python -m compileall -b -q src scripts start.py
        find . -name "*.py" -not -path "./tests/*" -delete
        find . -type d -name __pycache__ -prune -exec rm -rf {} +
        test -f src/book_engine/cli.pyc
        tar -czf /engine-artifact.tar.gz -C /engine .
    ')
    docker cp "$STAGE" "$CONTAINER:/engine"
    docker start -a "$CONTAINER" >/dev/null
    docker cp "$CONTAINER:/engine-artifact.tar.gz" "$OUTPUT"
    docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
else
    echo "warning: docker not found; using local python (must match image minor version)" >&2
    ( cd "$STAGE" \
      && python -m compileall -b -q src scripts start.py \
      && find . -name '*.py' -not -path './tests/*' -delete \
      && find . -type d -name __pycache__ -prune -exec rm -rf {} + \
      && test -f src/book_engine/cli.pyc )
    tar -czf "$OUTPUT" -C "$STAGE" .
fi

# Fail closed if any readable algorithm source survived. tests/ is expected to
# keep .py files (no algorithm there); src/ and scripts/ must not.
if tar -tzf "$OUTPUT" | grep -E '^\./(src|scripts)/.*\.py$' | grep -q .; then
    echo "error: .py sources remain under src/ or scripts/; refusing to pack" >&2
    rm -f "$OUTPUT"
    exit 1
fi

echo "[3/3] packed $OUTPUT"
echo
echo "artifact: $(cd "$(dirname "$OUTPUT")" && pwd)/$(basename "$OUTPUT")"
echo "size:     $(du -h "$OUTPUT" | cut -f1)"
echo "contents: $(tar -tzf "$OUTPUT" | wc -l) files, $(tar -tzf "$OUTPUT" | grep -c '\.pyc$') bytecode"
echo
echo "Next: docker build --build-arg ENGINE_ARTIFACT_PATH=$(basename "$OUTPUT") -f backend/Dockerfile ."
