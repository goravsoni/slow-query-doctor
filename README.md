# slow-query-doctor

An OpenSearch agent skill that diagnoses why **one specific search query** is
slow and hands back a **faster, verified rewrite**. It profiles the query with
the Profile API, localizes where the time goes (query vs aggregation vs fetch),
scans the query for the documented latency traps, and — because running a search
never changes the cluster — **runs the rewrite itself to prove the before/after
latency**. It never mutates index settings, mappings, or data; genuine config
changes are prescribed as a runbook the operator runs.

> Built for the OpenSearch Agent Skills Hackathon (US 2026). Apache-2.0.

## What it catches

Leading wildcards / `regexp`, `script`/`script_score` scoring, deep `from`/`size`
pagination, aggregations without `"size": 0`, high-cardinality `terms` aggs,
large `_source` fetch cost, uncacheable `now` ranges, and (with node APIs) search
thread-pool rejections and tripped circuit breakers.

## Layout

- `skills/slow-query-doctor/` — the shippable skill (SKILL.md + reference rules
  table + one read-only Python diagnostic). This subtree is what gets
  contributed upstream.
- `tests/` — a single-node OpenSearch (`docker-compose.yml`), four live
  scenarios, a deterministic scenario runner, and stdlib unit tests for the
  detection engine.
- `env/` — a fuller demo cluster (OpenSearch + Dashboards) with a seed script
  and `SETUP.md`, sized so the before/after latency delta is visible on camera.
- `evals/` — an LLM-as-judge harness (routing + rule-compliance cases) mirroring
  the official repo's eval structure.

## Run the verification loop

```bash
# 1. Bring up a local OpenSearch (always pass an explicit -p project name).
docker compose -p slow-query-doctor -f tests/docker-compose.yml up -d

# 2. Deterministic engine tests (no cluster, no claude):
python3 tests/test_slow_query.py

# 3. Live scenarios (seed -> profile -> assert findings) against the cluster:
OPENSEARCH_URL=http://localhost:9202 tests/run-diagnostic.sh deep-pagination
OPENSEARCH_URL=http://localhost:9202 tests/run-diagnostic.sh wildcard-scan
OPENSEARCH_URL=http://localhost:9202 tests/run-diagnostic.sh agg-no-size
OPENSEARCH_URL=http://localhost:9202 tests/run-diagnostic.sh clean-fast-query-pass

# 4. Tear down when done.
docker compose -p slow-query-doctor -f tests/docker-compose.yml down -v
```

The seed + profile halves are pure `curl`/Docker/`python3` and run anywhere;
invoking the skill through Claude Code (see `env/SETUP.md`) needs a working
`claude` CLI. The LLM-as-judge evals additionally need AWS Bedrock access.

## The diagnostic

```bash
# profile one query and localize the slowness (read-only):
python3 skills/slow-query-doctor/scripts/slow_query.py profile \
  --url "$OPENSEARCH_URL" --index <name> --query-file q.json [--nodes] [--json]

# prove a rewrite is faster (runs both several times; read-only):
python3 skills/slow-query-doctor/scripts/slow_query.py compare \
  --url "$OPENSEARCH_URL" --index <name> --before slow.json --after fast.json
```
