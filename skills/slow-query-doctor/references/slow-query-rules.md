# Slow-query rules — reads, thresholds, and the remediation runbook

Loaded on demand from [SKILL.md](../SKILL.md) Phases 1–4. Every command is a
`curl` against a generic REST path that works on any OpenSearch 1.x+
distribution. `$OPENSEARCH_URL` is the cluster endpoint
(fall back to `$ES`).

The numeric thresholds below are **defaults that match
`scripts/slow_query.py`** and are configurable (`--threshold key=value`). They
are guidance grounded in OpenSearch documentation, not hard limits. **If a
construct, parameter, or threshold is not documented here, fetch the current doc
at `docs.opensearch.org` and cite the URL — never guess** (SKILL.md Critical
Rule 3).

Two fix classes, and the line the skill never crosses:

- **Query rewrites are read-only.** Running a search — including the rewritten
  one and `_search?profile=true` — never changes cluster state, so the skill may
  run these itself to *prove* a rewrite is faster (Phase 4).
- **Index/config changes mutate** (mappings, settings, force-merge, replicas).
  The skill only *prints* these as a runbook; the operator runs them.

> Doc URLs were verified to resolve on 2026-08-16; re-check before submission.

---

## The reads (all read-only)

```bash
# WHERE the time goes — the Profile API annotates each query component with
# time_in_nanos. Add "profile": true to the search body:
curl -s -X GET "$OPENSEARCH_URL/<index>/_search" -H 'Content-Type: application/json' -d '{
  "profile": true, "query": { ... your query ... }
}'
# The request's own wall time:
#   the top-level "took" (ms) in any _search response.
```

Node-level search pressure (needs node APIs; unavailable on Serverless):

```bash
curl -s "$OPENSEARCH_URL/_nodes/hot_threads?threads=3"                       # who is burning CPU
curl -s "$OPENSEARCH_URL/_nodes/stats/thread_pool?filter_path=nodes.*.name,nodes.*.thread_pool.search"
curl -s "$OPENSEARCH_URL/_nodes/stats/breaker?filter_path=nodes.*.name,nodes.*.breakers"
```

- Profile API: https://docs.opensearch.org/latest/api-reference/profile/
- Search `took` and request structure: https://docs.opensearch.org/latest/api-reference/search/
- hot_threads: https://docs.opensearch.org/latest/api-reference/nodes-apis/nodes-hot-threads/
- Nodes stats (thread_pool, breakers): https://docs.opensearch.org/latest/api-reference/nodes-apis/nodes-stats/

## Thresholds

| Signal | Threshold | Severity |
|---|---|---|
| request `took` | ≥ 2000 ms | critical |
| request `took` | ≥ 500 ms | warning |
| one profile phase (query / aggregation / fetch) share of profiled time | ≥ 50% | warning (hotspot) |
| `from` + `size` | ≥ 10000 (`index.max_result_window` default) | critical (deep pagination) |
| `from` | ≥ 1000 | warning (deep pagination) |
| leading-wildcard / leading-`?` pattern | — | critical (full term scan) |
| `search` thread pool `rejected` on any node | > 0 | critical |
| any circuit breaker `tripped` on any node | > 0 | critical |

`index.max_result_window` default is **10000**. Docs:
https://docs.opensearch.org/latest/install-and-configure/configuring-opensearch/index-settings/

---

## Check A — Expensive query clauses (rewrite; read-only)

**Leading wildcard / `regexp`** — `{"wildcard":{"title":"*shoe"}}` scans the whole
term dictionary. Fix: index a `wildcard`-type field, or an edge-/n-gram analyzed
field, and query that instead. As a query-only mitigation, anchor the pattern
(avoid a leading `*`).
Docs: https://docs.opensearch.org/latest/field-types/supported-field-types/wildcard/
and https://docs.opensearch.org/latest/analyzers/token-filters/edge-ngram/

**`script` / `script_score`** — a script runs per candidate doc and cannot be
cached. Fix: precompute the signal into a numeric field and use
`function_score` `field_value_factor`, or model relevance boosts as a
`rank_feature` / `rank_features` field.
```json
{ "query": { "function_score": {
    "query": { "match": { "title": "shoe" } },
    "field_value_factor": { "field": "popularity", "modifier": "log1p" }
} } }
```
Docs: https://docs.opensearch.org/latest/query-dsl/compound/function-score/ and
https://docs.opensearch.org/latest/field-types/supported-field-types/rank-features/

**`fuzzy` / high edit distance** — expands to every term within the distance.
Fix: cap fuzziness at `AUTO` and set `max_expansions`; prefer a dedicated
autocomplete field for typo-tolerance.
Docs: https://docs.opensearch.org/latest/query-dsl/term/fuzzy/

**`query_string` with wildcards** — can silently expand to costly terms. Fix:
constrain `fields`, disable `allow_leading_wildcard`, or move to structured
`match`/`term` clauses.
Docs: https://docs.opensearch.org/latest/query-dsl/full-text/query-string/

## Check B — Deep pagination (rewrite; read-only)

`from`/`size` deep paging forces the coordinating node to collect and sort every
doc up to `from` on every shard. Beyond `index.max_result_window` (10000) it is
rejected outright. Fix: `search_after` with a point-in-time (PIT).
```bash
# 1) open a PIT (read-only; it is a search context, not a data change):
curl -s -X POST "$OPENSEARCH_URL/<index>/_search/point_in_time?keep_alive=1m"
# 2) page with search_after + the PIT id, sorting on a tiebreaker (e.g. _id):
curl -s -X GET "$OPENSEARCH_URL/_search" -H 'Content-Type: application/json' -d '{
  "size": 100, "query": { "match_all": {} },
  "pit": { "id": "<pit_id>", "keep_alive": "1m" },
  "sort": [ { "@timestamp": "asc" }, { "_id": "asc" } ],
  "search_after": [ <last_sort_values> ]
}'
```
Docs: https://docs.opensearch.org/latest/search-plugins/searching-data/paginate/
(Point-in-time & search_after)

## Check C — Aggregation cost (rewrite + config)

**Fetching hits you do not need** — an agg-only request with `size` unset still
fetches 10 hits. Fix (read-only rewrite): `"size": 0`. It skips the fetch phase
and makes the request shard-request-cacheable.

**High-cardinality `terms` agg on a `text` field** — loads fielddata into heap
(and can trip the fielddata breaker). Fix: aggregate on the `keyword` sub-field.
For a keyword field aggregated repeatedly, **the operator** can enable
`eager_global_ordinals` so ordinals build at refresh instead of per query
(a mapping change — mutating, so the operator runs it):
```bash
curl -s -X PUT "$OPENSEARCH_URL/<index>/_mapping" -H 'Content-Type: application/json' -d '{
  "properties": { "<field>": { "type": "keyword", "eager_global_ordinals": true } }
}'
```
Effect: faster repeated terms aggs; slightly slower refresh. Reverse: set
`eager_global_ordinals` back to `false`.
Docs: https://docs.opensearch.org/latest/aggregations/ and
https://docs.opensearch.org/latest/field-types/supported-field-types/keyword/

## Check D — Fetch cost (rewrite; read-only)

A large `_source` or many script/stored fields make the fetch phase dominate.
Fix: return only what you render.
```json
{ "_source": ["id", "title", "price"], "query": { "match": { "title": "shoe" } } }
```
`docvalue_fields` for already-doc-valued fields avoids `_source` reparsing.
Docs: https://docs.opensearch.org/latest/api-reference/search/ (source filtering)

## Check E — Caching (rewrite; read-only)

A `range` bound of `now` (unrounded) changes every millisecond, so the shard
request cache never hits. Fix: round with date math — `now/d`, `now/h`. Combine
with `"size": 0` to make dashboard/agg queries cacheable.
Docs: https://docs.opensearch.org/latest/api-reference/search/ (request cache) and
https://docs.opensearch.org/latest/field-types/supported-field-types/date/ (date math)

## Check F — Node-level search pressure (config / capacity; operator acts)

These are cluster conditions, not a single query's plan — surface them and hand
off; do not tune thread pools or force-merge yourself. Measure them with the
bundled diagnostic (no query needed):

```bash
python3 scripts/slow_query.py nodes --url "$OPENSEARCH_URL"
```

- **`search` thread-pool rejections** — the search queue overflowed. The query
  fan-out or cost is too high, or the cluster is under-provisioned. Reduce
  per-query cost (Checks A–E), reduce shard fan-out, or add search capacity.
  Docs: https://docs.opensearch.org/latest/tuning-your-cluster/performance/
- **Circuit breaker tripped** — a request exceeded a memory budget. The breaker
  name localizes it (`fielddata` → terms agg/sort on `text`; `request` → huge
  aggregation). Fix the offending query (Checks A, C) rather than raising the
  breaker limit.
  Docs: https://docs.opensearch.org/latest/api-reference/nodes-apis/nodes-stats/
- **Too many segments** (many small refreshes) inflate per-query work. A
  force-merge to fewer segments on a **read-only / rolled-over** index is an
  operator action (it is I/O heavy and must never run on an actively-written
  index):
  ```bash
  curl -s -X POST "$OPENSEARCH_URL/<index>/_forcemerge?max_num_segments=1"
  ```
  Docs: https://docs.opensearch.org/latest/api-reference/index-apis/force-merge/

---

## Looking up current docs

If a query parameter, setting, or threshold is not covered above, browse
`https://docs.opensearch.org/latest/` (query DSL under `/query-dsl/`, search
under `/api-reference/search/`, profiling under `/api-reference/profile/`,
tuning under `/tuning-your-cluster/`) and cite the exact URL in your finding.
Never invent a parameter or claim one does not exist without checking (SKILL.md
Critical Rule 3).
