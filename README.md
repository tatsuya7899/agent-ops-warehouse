# agent-ops-warehouse

Operational telemetry for a one-person AI-agent organization — a BigQuery warehouse, managed entirely by Terraform, running entirely inside GCP's free tier.

I run a small organization of AI agents (7 role-defined agents under written governance rules) that ships articles, code, and reviews. This repository is the instrument panel for that organization: what it commits, what it publishes, what its review gates catch, and what it costs — as queryable tables instead of anecdotes.

> **Fork-and-deploy**: `terraform apply` + `python -m loader` gives you the same warehouse in *your* GCP project, on the free tier, in minutes. This repo is a template, not just a diary.

## Why this exists

People increasingly run AI-agent setups, but almost nobody *measures* them. DevOps has observability stacks; personal AgentOps has nothing. This is a reference implementation: small, free, reproducible, and honest about its trade-offs.

## Architecture

```
Local machine (system of record)              GCP (derived, private)
┌──────────────────────────┐   manual      ┌─────────────────────────────┐
│ git history · Markdown    │  batch load   │ BigQuery                    │
│ publishing logs · KPIs    │──────────────▶│  raw → staging → marts      │──▶ Looker Studio
│ session aggregates        │   (free)      │  (all Terraform-managed)    │
└──────────────────────────┘               └──────────┬──────────────────┘
        ▲                                             │ VECTOR_SEARCH (brute force)
   weekly trigger,                          ┌─────────▼─────────────────┐
   human-initiated                          │ Cloud Run: FastAPI (P3)   │◀── Gemini API (free tier)
                                            └───────────────────────────┘
```

Design decisions worth stealing (or arguing with):

- **The system of record is the local machine, not the warehouse.** Every BigQuery table is derived data, rebuildable from sources by re-running the loader. That is why infrastructure is *recreated*, never imported, and why losing the warehouse costs nothing.
- **Honesty about scale**: this data is under 10 MB. By size alone, DuckDB would be the right tool. BigQuery wins on *distribution* requirements — zero-ops hosting, shareable dashboards, a remote SQL backend for the RAG API, and a permanent free tier. When the requirements change, the answer changes.
- **Governance as code, all the way down.** The organization is governed by written rules; the infrastructure follows the same principle. `terraform plan` is the review gate, drift detection is the deviation alarm, and the budget alert is a guardrail in code.
- **Vector search without a vector DB**: the RAG phase uses BigQuery `VECTOR_SEARCH` over an audited corpus of published articles. At this corpus size a vector index cannot even be created (5,000-row minimum) — so this is brute-force search, stated plainly, and the interesting part is the governed corpus, not retrieval benchmarks.
- **Append-only raw layer** with a load ledger (`raw_load_runs`) that records what was loaded *and what was excluded* — exclusions are auditable in the public loader code as an allowlist.

## What is deliberately not here

| Not built | Why |
|---|---|
| Airflow / Dagster | A weekly, human-triggered load needs no orchestrator |
| GKE / always-on VMs | Cost sources; Cloud Run covers the API phase |
| Streaming ingestion | Weekly batch; streaming inserts also cost money |
| A dedicated vector DB | One engine (BigQuery) for analytics *and* retrieval is the point |
| Unattended schedulers | Loads are human-initiated by design — a governance choice, not a limitation |

## Privacy boundary

Sources are personal repositories and personal logs only. Session telemetry is aggregated locally — counts per day, never content. Commit subjects are loaded for private analysis but never rendered on public surfaces; sample data in tests is synthetic. Employer information is excluded by an allowlist you can read in the loader.

## Quickstart

```bash
# 1. Infrastructure (needs gcloud auth application-default login)
cd terraform
echo 'project_id = "your-project"' > terraform.tfvars
terraform init && terraform apply

# 2. Load your own telemetry
python -m loader --repos ~/your-repos/* --out out/
for t in git_commits articles; do
  bq load --source_format=NEWLINE_DELIMITED_JSON --replace raw.$t out/raw_$t.ndjson
done
```

Free-tier envelope: BigQuery sandbox works without a card (tables expire in 60 days); enabling billing removes the expiration — visible here as Terraform drift, which is exactly how a checklist item should be encoded.

## Development

```bash
pytest -q          # 36+ tests, TDD-first
ruff check .       # lint
terraform fmt -check && terraform validate
```

CI runs all of the above plus tflint on every push.

## Roadmap

- **P2**: dbt Core for staging/marts (tests as data acceptance gates, generated lineage docs), Looker Studio dashboards
- **P3**: FastAPI + Cloud Run RAG over the published-article corpus (Gemini free tier, Bearer auth)

## Built in public

The build log is published as articles (Japanese) — linked here as they ship.
