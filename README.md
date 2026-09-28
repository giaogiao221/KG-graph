# KG-graph

Extraction Engine and Review Platform

This release directory contains the extraction engine source and the complete web-based extraction and review platform. It was exported without changing the original project or its extraction algorithms.

## Included components

- `engine/`: rule-based text/table extraction, LLM enhancement, ontology and policy configuration, fixed 59-column exports, command-line entry points and unit tests.
- `backend/`: FastAPI services, PostgreSQL migrations, Celery jobs, access control, review/versioning and exports.
- `frontend/`: Vue/TypeScript source, tests and the existing prebuilt `dist/` used by the Dockerfile.
- `deploy/`: Docker Compose, Nginx and an optional mock model endpoint for tests.
- `examples/demo_outputs/`: selected existing outputs for `engine/books/demo_energetic.md`. These are demonstration results, not the manuscript's complete experimental dataset or human-audit dataset.
- `engine-artifact.tar.gz`: the existing compiled engine package required by the unchanged Docker build. The readable engine source is also included under `engine/`.

Real credentials, uploaded documents, databases, caches, development screenshots, planning notes and local output directories are excluded. Runtime policy and ontology configuration files remain included.

## Run the engine independently

Use Python 3.11 as specified by the existing engine instructions. From `engine/`:

```console
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements-lock.txt
.venv/Scripts/python.exe start.py --skip-v106
```

On Linux/macOS use `.venv/bin/python` instead. This command runs the existing unit tests and rule extraction on the demonstration input. Outputs are written to `engine/runs/`.

For LLM enhancement, configure environment variables following `engine/config.env.example`; do not publish the resulting `config.env`. See `engine/README.md` for the existing pipeline options.

## Run the web platform

The unchanged Dockerfiles use Python 3.12 engine bytecode and the included frontend build. Copy `.env.example` to `.env`, fill in independent secrets and the initial administrator credentials, then run from the repository root:

```console
docker compose --env-file .env -f deploy/docker-compose.yml up -d --build
docker compose --env-file .env -f deploy/docker-compose.yml exec api alembic upgrade head
docker compose --env-file .env -f deploy/docker-compose.yml exec api python -m app.auth.bootstrap
```

Open `http://localhost:8080/`. Configure your own model API endpoint and credentials inside the platform for LLM extraction. See `docs/operations.md` for operations and tests.

If you change frontend source, rebuild its runtime files using the Node version compatible with `frontend/package-lock.json`:

```console
cd frontend
npm ci
npm run build
```

If you change engine source, regenerate the existing Docker artifact using Bash and Docker:

```console
bash tools/build-engine-artifact.sh engine engine-artifact.tar.gz
```

The original Dockerfiles and application code are preserved. This directory is a curated export; a complete clean-environment deployment acceptance test has not been performed during file preparation.

## Source, data and licensing

The existing platform MIT license is retained in `LICENSE`. The original copyright and license notices are preserved. The rights and license scope of engine resources and third-party material should be established by their owners before public distribution.

Source-code distribution does not grant permission to redistribute copyrighted books or manuscript datasets. Users supply input documents they are entitled to process. The bundled demonstration input and reference outputs are separate from full research data.

## Export verification

- Engine unit tests: 318 passed.
- Platform extraction and 59-column schema tests: 125 passed, 3 skipped.
- Tests used the existing local Python environments with UTF-8 mode enabled; they do not constitute a fresh dependency installation or complete Docker deployment test.
- All four bundled reference TSV outputs have 59 columns with consistent row widths.
- Original source files were hash-checked before and after verification and remained unchanged.
