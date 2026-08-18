#!/usr/bin/env bash
#
# Eval harness: seed a customer ENVIRONMENT, run each prompt through the agent
# (headless Claude Code) with the slow-query-doctor skill installed, and capture
# EXACTLY what the agent did — input, every tool call + args + result, final
# answer, usage. One transcript per case into a timestamped, per-environment dir.
#
#   evals/run-evals.sh --env shop                     # seed env, run all cases
#   evals/run-evals.sh --env shop --case wildcard-slow
#   evals/run-evals.sh                                # run against whatever is seeded
#   MODEL='...' OPENSEARCH_URL='http://localhost:9202' evals/run-evals.sh --env shop
#
# Environments live in evals/environments/<name>.py. Cases live in evals/cases.jsonl.
# Requires: `claude` on PATH, the skill installed at <repo>/.claude/skills/, a
# reachable cluster. Read-only skill, but runs bypassPermissions so the agent
# doesn't hang on tool prompts — only point it at a dev cluster.
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$DIR/../.." && pwd)"                 # evals/ -> slow-query-doctor/ -> repo root
MODEL="${MODEL:-global.anthropic.claude-opus-4-8[1m]}"
OPENSEARCH_URL="${OPENSEARCH_URL:-http://localhost:9202}"
ENV_NAME=""
ONLY=""
JUDGE=1
while [ $# -gt 0 ]; do
  case "$1" in
    --env)      ENV_NAME="$2"; shift 2;;
    --case)     ONLY="$2"; shift 2;;
    --model)    MODEL="$2"; shift 2;;
    --no-judge) JUDGE=0; shift;;
    *) echo "unknown arg: $1"; exit 2;;
  esac
done

# 1. Seed the requested environment (a reproducible customer cluster state).
if [ -n "$ENV_NAME" ]; then
  ENV_PY="$DIR/environments/$ENV_NAME.py"
  [ -f "$ENV_PY" ] || { echo "no environment '$ENV_NAME' (looked for $ENV_PY)"; exit 2; }
  echo "== seeding environment: $ENV_NAME =="
  ( cd "$DIR/environments" && python3 "$ENV_NAME.py" --url "$OPENSEARCH_URL" )
  sleep 2
fi

TS="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="$DIR/results/${TS}${ENV_NAME:+-$ENV_NAME}"
mkdir -p "$OUT"
printf '{"env":"%s","model":"%s","endpoint":"%s","ts":"%s"}\n' \
  "${ENV_NAME:-current}" "$MODEL" "$OPENSEARCH_URL" "$TS" > "$OUT/manifest.json"
echo "env=${ENV_NAME:-current}  model=$MODEL  endpoint=$OPENSEARCH_URL"
echo "results -> $OUT"
echo

# 2. Run each case through the agent and capture the full event stream.
while IFS= read -r line; do
  [ -z "$line" ] && continue
  id=$(printf '%s' "$line" | python3 -c "import json,sys;print(json.load(sys.stdin)['id'])")
  prompt=$(printf '%s' "$line" | python3 -c "import json,sys;print(json.load(sys.stdin)['prompt'])")
  if [ -n "$ONLY" ] && [ "$ONLY" != "$id" ]; then continue; fi
  printf '%s' "$line" | python3 -c "import json,sys;print(json.load(sys.stdin).get('expect',''))" > "$OUT/$id.expect.txt"

  echo "== $id =="
  ( cd "$REPO" && OPENSEARCH_URL="$OPENSEARCH_URL" claude -p "$prompt" \
        --model "$MODEL" \
        --output-format stream-json --verbose \
        --permission-mode bypassPermissions </dev/null ) \
      > "$OUT/$id.stream.jsonl" 2> "$OUT/$id.stderr" \
      || echo "  ! claude exited non-zero — see $OUT/$id.stderr"

  if python3 "$DIR/summarize.py" "$OUT/$id.stream.jsonl" "$prompt" > "$OUT/$id.md" 2>>"$OUT/$id.stderr"; then
    echo "  -> $OUT/$id.md"
  else
    echo "  ! summarize failed — raw stream at $OUT/$id.stream.jsonl"
  fi
done < "$DIR/cases.jsonl"

# 3. LLM-as-judge (Bedrock Converse, fanned across eval accounts).
if [ "$JUDGE" = "1" ]; then
  echo
  echo "== LLM-as-judge (Bedrock, fan-out across eval accounts) =="
  node "$DIR/judge.mjs" --run "$(basename "$OUT")" 2>&1 | tail -20 || echo "  ! judge step failed — re-run: node evals/judge.mjs --run $(basename "$OUT")"
fi

echo
echo "done: $OUT"
