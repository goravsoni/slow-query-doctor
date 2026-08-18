# Customer environment — OpenSearch + Dashboards + Claude Code

Stand up a realistic OpenSearch cluster with the UI, seed a shop dataset big
enough that slow queries are *measurably* slow, and drive the
`slow-query-doctor` skill from Claude Code — including the before/after latency
proof that makes the demo land.

## Prerequisites

- **Docker Desktop** (macOS/Windows) or Docker Engine (Linux).
  https://docs.docker.com/desktop/
- **python3** — for the seed script and the diagnostic. No third-party packages.
- **Claude Code** — run it from the repo root so it discovers the project skill.

## 1. Start the cluster + UI

```bash
docker compose -p slow-query-doctor-env -f slow-query-doctor/env/docker-compose.yml up -d
```

Always pass `-p <project>`. Wait ~30–60s, then confirm:

```bash
curl -s http://localhost:9200/_cluster/health | python3 -m json.tool
```

- OpenSearch API → http://localhost:9200
- **OpenSearch Dashboards (the UI)** → http://localhost:5601

## 2. Seed the shop dataset

```bash
python3 slow-query-doctor/env/seed-customer-data.py --docs 50000
```

Creates `shop-products` (50k docs) with a high-cardinality `brand` keyword, an
analyzed `title`, categories, price, and popularity — enough to make each
anti-pattern show a real latency delta.

## 3. Use the skill from Claude Code

The skill is installed for this project (symlinked at
`.claude/skills/slow-query-doctor/`). Launch Claude Code from the repo root and
point the skill at the cluster:

```bash
export OPENSEARCH_URL=http://localhost:9200   # the skill's Phase 0 reads this
claude
```

Then paste a slow query and ask why it's slow — the skill profiles it, names the
cause, rewrites it, and (because searches are read-only) runs the rewrite to show
the real before/after `took`:

- "This search on `shop-products` is slow: `{ "query": { "wildcard": { "title": { "value": "*shoe" } } } }` — why, and how do I fix it?"
- "Paginating my results with `from: 40000` is crawling. Profile it and tell me what to run."
- "My category facet query feels heavy: `{ "query": { "match_all": {} }, "aggs": { "by_brand": { "terms": { "field": "brand" } } } }`."

## 4. The before/after proof (the demo money shot)

The `compare` command runs a slow query and its rewrite several times and prints
the median `took` for each — an objective, on-camera number, not a claim:

```bash
# leading-wildcard scan  vs  match on the analyzed field
echo '{ "size": 10, "query": { "wildcard": { "title": { "value": "*shoe" } } } }' > /tmp/slow.json
echo '{ "size": 10, "query": { "match":    { "title": "shoe" } } }'               > /tmp/fast.json
python3 slow-query-doctor/skills/slow-query-doctor/scripts/slow_query.py \
  compare --url http://localhost:9200 --index shop-products --before /tmp/slow.json --after /tmp/fast.json

# deep pagination  vs  search_after + PIT (open a PIT first; see references/slow-query-rules.md)
echo '{ "from": 40000, "size": 20, "query": { "match_all": {} } }' > /tmp/slow-page.json
python3 slow-query-doctor/skills/slow-query-doctor/scripts/slow_query.py \
  profile --url http://localhost:9200 --index shop-products --query-file /tmp/slow-page.json
```

## 5. (Optional) Wire the OpenSearch MCP server into Claude Code

The skill works with plain `curl` + the bundled `python3` diagnostic and needs no
MCP server. To route the agent through MCP tools instead, add `.mcp.json` at the
repo root (needs `uv`/`uvx`):

```json
{
  "mcpServers": {
    "opensearch-mcp-server": {
      "command": "uvx",
      "args": ["opensearch-mcp-server-py@latest"],
      "env": {
        "OPENSEARCH_URL": "http://localhost:9200",
        "OPENSEARCH_SSL_VERIFY": "false",
        "FASTMCP_LOG_LEVEL": "ERROR"
      }
    }
  }
}
```
Then restart Claude Code (or reconnect MCP servers).

## 6. Run the automated integration check (no agent needed)

Proves the diagnostic against a live cluster deterministically (uses its own
`sqd-*` indices on the test cluster; see `tests/`):

```bash
docker compose -p slow-query-doctor -f slow-query-doctor/tests/docker-compose.yml up -d
OPENSEARCH_URL=http://localhost:9202 slow-query-doctor/tests/run-diagnostic.sh deep-pagination
OPENSEARCH_URL=http://localhost:9202 slow-query-doctor/tests/run-diagnostic.sh wildcard-scan
OPENSEARCH_URL=http://localhost:9202 slow-query-doctor/tests/run-diagnostic.sh agg-no-size
OPENSEARCH_URL=http://localhost:9202 slow-query-doctor/tests/run-diagnostic.sh clean-fast-query-pass
```

## Teardown

```bash
docker compose -p slow-query-doctor-env -f slow-query-doctor/env/docker-compose.yml down -v
```
