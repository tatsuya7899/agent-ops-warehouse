#!/usr/bin/env bash
# Thin curl wrapper around the RAG API's POST /query endpoint (SPEC
# Section 4.4b: "専用チャットUI・フロントエンドは対象外" -- this is a client
# for the API, not a UI). Reads the deployment URL and Bearer token from
# environment variables (never hardcoded), and pretty-prints the JSON
# response with jq.
#
# Usage:
#   export RAG_API_URL="https://rag-api-xxxxx-uc.a.run.app"
#   export RAG_API_TOKEN="<the value stored in Secret Manager's API_TOKEN>"
#   scripts/query_articles.sh "質問文"
set -euo pipefail

if [ $# -eq 0 ]; then
  echo "usage: $0 <question>" >&2
  exit 1
fi

if [ -z "${RAG_API_URL:-}" ]; then
  echo "error: RAG_API_URL is not set" >&2
  exit 1
fi

if [ -z "${RAG_API_TOKEN:-}" ]; then
  echo "error: RAG_API_TOKEN is not set" >&2
  exit 1
fi

question="$1"
top_k="${2:-5}"

curl -sS -X POST "${RAG_API_URL%/}/query" \
  -H "Authorization: Bearer ${RAG_API_TOKEN}" \
  -H "Content-Type: application/json" \
  -d "$(jq -n --arg q "$question" --argjson k "$top_k" '{question: $q, top_k: $k, summarize: true}')" \
  | jq .
