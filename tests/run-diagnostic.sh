#!/usr/bin/env bash
#
# Live integration test for the read-only diagnostic — no `claude` needed.
# Seeds a scenario's index + docs on a running OpenSearch, profiles the
# scenario's query with slow_query.py, and asserts the findings match the
# scenario's `diagnostic_expect`. Proves the detection engine on a REAL cluster.
#
#   docker compose -f tests/docker-compose.yml up -d      # once
#   tests/run-diagnostic.sh deep-pagination
#   tests/run-diagnostic.sh wildcard-scan
#   tests/run-diagnostic.sh agg-no-size
#   tests/run-diagnostic.sh clean-fast-query-pass
#
# Each scenario deletes + recreates its own sqd-* index, so re-running one is safe.
set -euo pipefail

SC="${1:?usage: run-diagnostic.sh <deep-pagination|wildcard-scan|agg-no-size|clean-fast-query-pass>}"
BASE="${OPENSEARCH_URL:-http://localhost:9200}"
DIR="$(cd "$(dirname "$0")" && pwd)"
SCDIR="$DIR/scenarios/$SC"
SCRIPT="$DIR/../skills/slow-query-doctor/scripts/slow_query.py"
[ -d "$SCDIR" ] || { echo "no scenario '$SC' under $DIR/scenarios/"; exit 2; }

echo "== seeding '$SC' against $BASE =="
python3 - "$SCDIR/setup.json" "$BASE" <<'PY'
import json, sys, urllib.request, urllib.error
setup, base = json.load(open(sys.argv[1])), sys.argv[2].rstrip("/")

def req(method, path, data=None):
    r = urllib.request.Request(base + path, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    try:
        return urllib.request.urlopen(r, timeout=60).read()
    except urllib.error.HTTPError as e:
        if not (method == "DELETE" and e.code == 404):
            print(f"  ! setup {method} {path} -> HTTP {e.code}", file=sys.stderr)

_WORDS = ["shoe", "boot", "sandal", "sneaker", "loafer", "slipper", "cleat", "heel"]
_CATS = ["footwear", "apparel", "accessories", "outdoor", "sale", "clearance"]

def gen_bulk(index, n):
    lines = []
    for i in range(n):
        lines.append(json.dumps({"index": {"_index": index}}))
        lines.append(json.dumps({
            "title": f"{_WORDS[i % len(_WORDS)]} model {i}",
            "category": _CATS[i % len(_CATS)],
            "price": round(10 + (i % 200) * 0.5, 2),
            "popularity": i % 500,
            "in_stock": (i % 3 != 0),
            "@timestamp": f"2026-08-{1 + i % 15:02d}T00:00:00Z",
        }))
    return ("\n".join(lines) + "\n").encode()

for c in setup:
    if "bulk" in c:
        req("POST", "/_bulk?refresh=true", gen_bulk(c["index"], int(c["bulk"])))
    else:
        body = json.dumps(c["body"]).encode() if "body" in c else None
        req(c["method"], c["path"], body)
PY
sleep 1

echo "== profiling =="
META="$(python3 -c "import json;e=json.load(open('$SCDIR/expect.json'))['diagnostic_expect'];print(e['index'], e.get('query_file','query.json'))")"
INDEX="${META%% *}"; QFILE="${META##* }"
RESULT_JSON="$(mktemp)"
trap 'rm -f "$RESULT_JSON"' EXIT
python3 "$SCRIPT" profile --url "$BASE" --index "$INDEX" --query-file "$SCDIR/$QFILE" --json > "$RESULT_JSON" || true
cat "$RESULT_JSON"

echo "== asserting against diagnostic_expect =="
python3 - "$SCDIR/expect.json" "$RESULT_JSON" <<'PY'
import json, sys
expect = json.load(open(sys.argv[1]))["diagnostic_expect"]
result = json.load(open(sys.argv[2]))
kinds = {f["kind"] for f in result.get("findings", [])}
subtypes = {f.get("subtype") for f in result.get("findings", [])}
need_kinds = set(expect.get("finding_kinds", []))
need_subs = set(expect.get("expensive_subtypes", []))
verdict = result.get("verdict")
ok_kinds = need_kinds.issubset(kinds)
if need_kinds == set() and kinds:
    ok_kinds = False   # clean scenario: expected zero findings but got some
ok_subs = need_subs.issubset(subtypes)
ok_verdict = verdict in expect.get("verdict_in", [])
print(f"  expected kinds >= {sorted(need_kinds) or '(none)'}, got {sorted(kinds) or '(none)'} -> {'ok' if ok_kinds else 'FAIL'}")
if need_subs:
    print(f"  expected subtypes >= {sorted(need_subs)}, got {sorted(s for s in subtypes if s)} -> {'ok' if ok_subs else 'FAIL'}")
print(f"  expected verdict in {expect.get('verdict_in')}, got '{verdict}' -> {'ok' if ok_verdict else 'FAIL'}")
sys.exit(0 if (ok_kinds and ok_subs and ok_verdict) else 1)
PY
STATUS=$?
if [ "$STATUS" -eq 0 ]; then echo "PASS: $SC"; else echo "FAIL: $SC"; fi
exit "$STATUS"
