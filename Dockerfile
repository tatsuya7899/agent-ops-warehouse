# Cloud Run RAG API (SPEC-agent-ops-warehouse-rag-api_20260811.md Section
# 4.5). No multi-stage build: every dependency below is a pure-Python wheel
# (fastapi/uvicorn/google-cloud-bigquery/google-genai) -- there is no
# compiled-artifact size to shed by splitting build/runtime stages, and the
# SPEC's own posture favors simplicity over build-time optimization for a
# single-service, non-multi-tenant image (Section 3: "既存資産の再利用を優
# 先する").
FROM python:3.13-slim

WORKDIR /app

# Runtime dependencies only -- NOT `pip install .` (the local package
# itself). Verified 2026-08-12: `pip install .` fails in this repo because
# setuptools' flat-layout auto-discovery finds every top-level directory
# (out/, dbt/, terraform/, queries/, evidence/, logs/, api/, loader/) as an
# ambiguous "package" candidate and refuses to build a wheel
# ("Multiple top-level packages discovered in a flat-layout"). Installing
# the explicit dependency list instead sidesteps that entirely.
#
# Pinned to the same floor versions as pyproject.toml's `api` extra --
# keep these two lists in sync if that extra changes.
#
# google-genai is declared explicitly (not part of pyproject.toml's `api`
# extra before this file was added) because api/main.py's
# embed_question()/summarize_answer() call
# scripts.build_embeddings.build_gemini_client(), which does
# `from google import genai` at request time. Previously google-genai was
# only ever present transitively via the separate `dbt` extra
# (dbt-bigquery -> google-cloud-aiplatform -> google-genai) -- an extra
# this image never installs -- so every real /query request would have
# 503'd on ImportError while /health kept returning 200. Fixed alongside
# this Dockerfile by adding google-genai to pyproject.toml's `api` extra
# too (see that file's inline note) so this list and that one describe the
# same runtime.
RUN pip install --no-cache-dir \
    "fastapi>=0.115" \
    "uvicorn[standard]>=0.30" \
    "google-cloud-bigquery>=3.0" \
    "google-genai>=1.0"

# Source, limited to what api/main.py's import chain actually reaches at
# request time:
#   api/      -- the FastAPI app (api/main.py, api/query.py)
#   scripts/  -- build_gemini_client / call_embedding_api, imported by
#                api/main.py (SPEC Section 3: "既存資産の再利用を優先する")
#   loader/   -- scripts/build_embeddings.py imports loader.emit /
#                loader.extract_articles
# terraform/schemas/ is intentionally NOT copied: build_embeddings.py's
# DEFAULT_SCHEMA_PATH is only dereferenced by its --dry-run-chunks/main()
# CLI path (the offline embedding-build script), which this image's
# entrypoint (uvicorn api.main:app, the query-serving API) never calls.
COPY api/ api/
COPY scripts/ scripts/
COPY loader/ loader/

# Cloud Run injects PORT at runtime (its own container contract) -- the
# default of 8080 here is only for local `docker run` parity, matching
# SPEC Section 4.4's container_port. uvicorn resolves the "api.main:app"
# import string against the current working directory (/app), which is
# why no `pip install .` / PYTHONPATH wiring is needed above -- api/,
# scripts/, loader/ are plain subdirectories of /app with __init__.py
# files already in the repo.
ENV PORT=8080
EXPOSE 8080

CMD ["sh", "-c", "uvicorn api.main:app --host 0.0.0.0 --port ${PORT}"]
