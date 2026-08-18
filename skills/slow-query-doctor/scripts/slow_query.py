#!/usr/bin/env python3
# /// script
# requires-python = ">=3.7"
# dependencies = []
# ///
"""Read-only OpenSearch slow-query diagnostic.

Profiles a specific search against a running cluster, localizes where the time
goes (Profile API self-time by query component), statically scans the query DSL
for the well-known latency traps, and returns the raw measurements + computed
findings as JSON. It only issues search reads and node-stat GETs — it never
mutates the cluster or its data.

Two things it measures:
  1. WHERE the time went  — the Profile API breakdown (query vs aggregation vs
     fetch time, and the single dominant query component by self-time).
  2. WHY it is slow        — a static scan of the query body for the documented
     expensive constructs (leading wildcards, script scoring, deep pagination,
     agg-without-size, uncacheable now-ranges, ...), each tied to a fix in
     references/slow-query-rules.md.

`compare` runs a slow query and a candidate rewrite several times and reports
the before/after `took` — the objective proof that a rewrite is faster. Both
queries are ordinary searches, so this stays fully read-only.

Thresholds are defaults (see DEFAULTS) and mirror references/slow-query-rules.md.
Stdlib only: runs with `python3 slow_query.py ...` or `uv run`.

Usage:
  python3 slow_query.py profile --url http://localhost:9200 --index <name> --query-file q.json [--nodes] [--json]
  python3 slow_query.py compare --url http://localhost:9200 --index <name> --before slow.json --after fast.json [--runs 5]
"""
import argparse
import json
import re
import sys
import urllib.request
import urllib.error

# Default slow-query thresholds. Documented + doc-cited in
# references/slow-query-rules.md. Override any via `--threshold key=value`.
DEFAULTS = {
    "slow_took_ms": 500,          # per-request latency >= this -> warning
    "crit_took_ms": 2000,         # >= this -> critical
    "hotspot_fraction": 0.50,     # one phase/type >= this share of time -> hotspot
    "deep_from_warn": 1000,       # from >= this -> deep-pagination warning
    "max_result_window": 10000,   # from + size >= this -> critical (default OS/ES cap)
    "agg_no_size_default": 10,    # default hits fetched when size is unset
}

_SEV_RANK = {"info": 0, "warning": 1, "critical": 2}

# Query clauses that are documented latency traps. Maps the DSL key to a
# (subtype, severity, note) — the remediation for each lives in the reference.
_EXPENSIVE_KEYS = {
    "script_score": ("script_score", "warning",
                     "script_score runs a script per candidate doc and is uncacheable"),
    "script": ("script", "warning",
               "scripted scoring/filtering runs per doc and is uncacheable"),
    "regexp": ("regexp", "warning",
               "regexp runs a term automaton over the term dictionary"),
    "fuzzy": ("fuzzy", "warning",
              "fuzzy expands to many terms within the edit distance"),
    "more_like_this": ("more_like_this", "warning",
                       "more_like_this analyzes the input and issues many term lookups"),
    "wildcard": ("wildcard", "warning", "wildcard scans the term dictionary"),
    "prefix": ("prefix", "info", "prefix expands to every matching term"),
    "query_string": ("query_string", "info",
                     "query_string can expand to costly wildcard/regex terms"),
    "simple_query_string": ("simple_query_string", "info",
                            "simple_query_string can expand to costly terms"),
}


# --------------------------------------------------------------------------- #
# Pure functions (unit-tested against real-OpenSearch-shaped fixtures)
# --------------------------------------------------------------------------- #
def _leading_wildcard(value):
    """True when a wildcard/regexp pattern forces a full term-dictionary scan."""
    if not isinstance(value, str):
        return False
    return value.startswith("*") or value.startswith("?")


def _wildcard_value(spec):
    """Pull the pattern out of a wildcard/prefix clause in either DSL shape.

    {"field": "*foo"} or {"field": {"value": "*foo"}} / {"field": {"prefix": ..}}.
    """
    if not isinstance(spec, dict):
        return None
    for _field, v in spec.items():
        if isinstance(v, str):
            return v
        if isinstance(v, dict):
            return v.get("value") or v.get("prefix") or v.get("wildcard")
    return None


def _range_uses_unrounded_now(spec):
    """A range whose bound is `now` without date-math rounding defeats caching."""
    if not isinstance(spec, dict):
        return False
    for _field, bounds in spec.items():
        if not isinstance(bounds, dict):
            continue
        for key in ("gte", "gt", "lte", "lt"):
            val = bounds.get(key)
            if isinstance(val, str) and "now" in val and "/" not in val:
                return True
    return False


def scan_expensive_clauses(query, path="query"):
    """Recursively find documented expensive constructs in a query DSL subtree.

    Returns a list of raw findings (kind='expensive-clause'); severity/notes come
    from _EXPENSIVE_KEYS, with leading-wildcard and unrounded-now upgraded.
    """
    findings = []
    if isinstance(query, list):
        for i, item in enumerate(query):
            findings.extend(scan_expensive_clauses(item, "%s[%d]" % (path, i)))
        return findings
    if not isinstance(query, dict):
        return findings

    for key, spec in query.items():
        here = "%s.%s" % (path, key)
        if key in _EXPENSIVE_KEYS:
            subtype, sev, note = _EXPENSIVE_KEYS[key]
            if key in ("wildcard", "regexp") and _leading_wildcard(_wildcard_value(spec)):
                subtype, sev = subtype + "-leading", "critical"
                note = "leading-wildcard pattern forces a full term-dictionary scan"
            findings.append({
                "kind": "expensive-clause", "subtype": subtype, "severity": sev,
                "path": here, "message": note,
            })
        elif key == "range" and _range_uses_unrounded_now(spec):
            findings.append({
                "kind": "expensive-clause", "subtype": "range-unrounded-now",
                "severity": "info", "path": here,
                "message": "range bound uses `now` without rounding (e.g. now/d); "
                           "defeats the shard request cache",
            })
        # Recurse into nested structures regardless (bool/must/should/nested/...).
        findings.extend(scan_expensive_clauses(spec, here))
    return findings


def classify_pagination(from_, size, t=DEFAULTS):
    """Deep from/size pagination finding, or None."""
    from_ = from_ or 0
    size = 10 if size is None else size
    total = from_ + size
    if total >= t["max_result_window"]:
        return {
            "kind": "deep-pagination", "severity": "critical", "from": from_, "size": size,
            "message": ("from+size=%d reaches max_result_window=%d; the coordinating "
                        "node must collect and sort every doc up to `from`. Use "
                        "search_after + a point-in-time (PIT) instead"
                        % (total, t["max_result_window"])),
        }
    if from_ >= t["deep_from_warn"]:
        return {
            "kind": "deep-pagination", "severity": "warning", "from": from_, "size": size,
            "message": ("from=%d paginates deeply; cost grows with `from`. Prefer "
                        "search_after + PIT for stable, cheap deep paging" % from_),
        }
    return None


def classify_fetch(size, has_aggs, t=DEFAULTS):
    """Aggregation-only query that still fetches hits, or None."""
    if has_aggs and (size is None or size > 0):
        fetched = t["agg_no_size_default"] if size is None else size
        return {
            "kind": "fetch-without-size", "severity": "warning", "size": fetched,
            "message": ("query has aggregations but size is not 0, so it also fetches "
                        "%d hit(s) it likely does not need. Set \"size\": 0 — it skips "
                        "the fetch phase and makes the request shard-cacheable" % fetched),
        }
    return None


def _walk_query_selftime(nodes, acc):
    """Bucket each profile node's self-time (own time minus children) by type."""
    for n in nodes or []:
        if not isinstance(n, dict):
            continue
        t = n.get("time_in_nanos", 0) or 0
        children = n.get("children") or []
        child_sum = sum((c.get("time_in_nanos", 0) or 0) for c in children if isinstance(c, dict))
        typ = n.get("type") or n.get("name") or "unknown"
        acc[typ] = acc.get(typ, 0) + max(0, t - child_sum)
        _walk_query_selftime(children, acc)


def summarize_profile(profile):
    """Sum per-shard query/agg/collector/fetch/rewrite time and find the dominant
    query component by aggregated self-time. All times in nanoseconds."""
    q = agg = coll = fetch = rewrite = 0
    type_acc = {}
    for sh in (profile or {}).get("shards", []):
        for se in sh.get("searches", []) or []:
            for qn in se.get("query", []) or []:
                q += qn.get("time_in_nanos", 0) or 0
            rewrite += se.get("rewrite_time", 0) or 0
            for cn in se.get("collector", []) or []:
                coll += cn.get("time_in_nanos", 0) or 0
            _walk_query_selftime(se.get("query", []), type_acc)
        for an in sh.get("aggregations", []) or []:
            agg += an.get("time_in_nanos", 0) or 0
        fe = sh.get("fetch")
        if isinstance(fe, dict):
            fetch += fe.get("time_in_nanos", 0) or 0
    hotspot_type = max(type_acc, key=type_acc.get) if type_acc else None
    return {
        "query_ns": q, "aggregation_ns": agg, "collector_ns": coll,
        "fetch_ns": fetch, "rewrite_ns": rewrite,
        "hotspot_query_type": hotspot_type,
        "hotspot_query_type_ns": type_acc.get(hotspot_type, 0) if hotspot_type else 0,
    }


def classify_hotspot(summary, t=DEFAULTS):
    """Turn a profile summary into phase/type hotspot findings."""
    findings = []
    total = summary["query_ns"] + summary["aggregation_ns"] + summary["fetch_ns"]
    if total <= 0:
        return findings
    frac = t["hotspot_fraction"]
    if summary["aggregation_ns"] / total >= frac:
        findings.append({
            "kind": "profile-hotspot", "subtype": "aggregations", "severity": "warning",
            "share": round(summary["aggregation_ns"] / total, 2),
            "message": ("aggregations account for %.0f%% of profiled time; check for "
                        "high-cardinality terms aggs on text/analyzed fields and enable "
                        "eager_global_ordinals or aggregate on a keyword field"
                        % (100 * summary["aggregation_ns"] / total)),
        })
    if summary["fetch_ns"] / total >= frac:
        findings.append({
            "kind": "profile-hotspot", "subtype": "fetch", "severity": "warning",
            "share": round(summary["fetch_ns"] / total, 2),
            "message": ("the fetch phase accounts for %.0f%% of profiled time; a large "
                        "_source or many stored/script fields dominate. Fetch only what "
                        "you render (_source filtering / docvalue_fields)"
                        % (100 * summary["fetch_ns"] / total)),
        })
    if (summary["query_ns"] / total >= frac and summary["hotspot_query_type"]):
        findings.append({
            "kind": "profile-hotspot", "subtype": "query", "severity": "warning",
            "dominant_type": summary["hotspot_query_type"],
            "share": round(summary["query_ns"] / total, 2),
            "message": ("the query phase dominates (%.0f%%), most of it in %s. This is "
                        "the clause to rewrite or cache"
                        % (100 * summary["query_ns"] / total, summary["hotspot_query_type"])),
        })
    return findings


def classify_took(took_ms, t=DEFAULTS):
    """Latency finding from the request's own `took`, or None."""
    if took_ms is None:
        return None
    if took_ms >= t["crit_took_ms"]:
        sev = "critical"
    elif took_ms >= t["slow_took_ms"]:
        sev = "warning"
    else:
        return None
    return {
        "kind": "slow-took", "severity": sev, "took_ms": took_ms,
        "message": "request took %d ms (threshold %d ms)" % (took_ms, t["slow_took_ms"]),
    }


def classify_cluster(thread_pool_nodes, breaker_nodes):
    """Node-level search-pressure findings from thread_pool + breaker stats."""
    findings = []
    for name, tp in (thread_pool_nodes or {}).items():
        rejected = (tp.get("search") or {}).get("rejected", 0) or 0
        queue = (tp.get("search") or {}).get("queue", 0) or 0
        if rejected > 0:
            findings.append({
                "kind": "thread-pool-rejections", "severity": "critical", "node": name,
                "rejected": rejected, "queue": queue,
                "message": ("node '%s' has rejected %d search request(s) (queue=%d); the "
                            "search thread pool is saturated — reduce query cost/fan-out "
                            "or add search capacity" % (name, rejected, queue)),
            })
    for name, breakers in (breaker_nodes or {}).items():
        for bname, b in (breakers or {}).items():
            tripped = b.get("tripped", 0) or 0
            if tripped > 0:
                findings.append({
                    "kind": "circuit-breaker-tripped", "severity": "critical",
                    "node": name, "breaker": bname, "tripped": tripped,
                    "message": ("node '%s' '%s' breaker has tripped %d time(s); a request "
                                "exceeded the memory budget — the '%s' breaker points at "
                                "the cause (e.g. fielddata on text, huge aggregations)"
                                % (name, bname, tripped, bname)),
                })
    return findings


def worst_severity(findings):
    if not findings:
        return "fast"
    return max((f["severity"] for f in findings), key=lambda s: _SEV_RANK.get(s, 0))


# --------------------------------------------------------------------------- #
# Read-only IO (search reads + node-stat GETs; never mutates)
# --------------------------------------------------------------------------- #
def _get(base_url, path, timeout=20):
    req = urllib.request.Request(base_url.rstrip("/") + path, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _search(base_url, index, body, timeout=60):
    """POST a search. A search reads only — it never changes cluster state."""
    url = base_url.rstrip("/") + "/" + index.strip("/") + "/_search"
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def detect_distribution(base_url):
    """Return (distribution, version, node_apis_available)."""
    try:
        root = _get(base_url, "/")
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise SystemExit("auth required: pass credentials; this diagnostic does not assume them")
        raise SystemExit("could not reach %s: HTTP %d" % (base_url, e.code))
    except Exception as e:  # noqa: BLE001 - surface any connection error plainly
        raise SystemExit("could not reach %s: %s" % (base_url, e))
    version = root.get("version") or {}
    node_apis = "number" in version  # Serverless exposes no node/cluster APIs
    return version.get("distribution", "elasticsearch"), version.get("number"), node_apis


def _read_body(args):
    if getattr(args, "query_file", None):
        with open(args.query_file) as fh:
            return json.load(fh)
    if getattr(args, "query", None):
        return json.loads(args.query)
    data = sys.stdin.read().strip()
    if not data:
        raise SystemExit("no query: pass --query-file, --query, or pipe a JSON body on stdin")
    return json.loads(data)


def _run_profiled_search(base_url, index, body):
    """Run the search once with profiling on; return (took_ms, profile, error)."""
    req_body = dict(body)
    req_body["profile"] = True
    try:
        resp = _search(base_url, index, req_body)
        return resp.get("took"), resp.get("profile"), None
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8")[:600]
        except Exception:  # noqa: BLE001
            pass
        return None, None, "search failed: HTTP %d %s" % (e.code, detail)
    except Exception as e:  # noqa: BLE001
        return None, None, "search failed: %s" % e


def cmd_profile(args, thresholds):
    dist, version, node_apis = detect_distribution(args.url)
    body = _read_body(args)
    query = body["query"] if "query" in body else body
    aggs = body.get("aggs") or body.get("aggregations")
    result = {
        "distribution": dist, "version": version, "index": args.index,
        "node_apis_available": node_apis, "findings": [],
    }

    # 1. Static DSL scan — deterministic, needs no profiling run.
    static = scan_expensive_clauses(query)
    static.extend([f for f in [classify_pagination(body.get("from"), body.get("size"), thresholds),
                               classify_fetch(body.get("size"), bool(aggs), thresholds)] if f])
    result["findings"].extend(static)

    # 2. Profiled run — where the time actually went.
    took, profile, err = _run_profiled_search(args.url, args.index, body)
    result["took_ms"] = took
    if err:
        result["search_error"] = err  # e.g. from+size over the window: itself a finding
    if profile:
        summary = summarize_profile(profile)
        result["profile_summary"] = summary
        # A hotspot is only worth reporting once the query is actually slow — a
        # fast query's dominant phase is not a problem. Gate on the slow threshold
        # so a healthy query stays finding-free.
        if took is not None and took >= thresholds["slow_took_ms"]:
            result["findings"].extend(classify_hotspot(summary, thresholds))
    took_finding = classify_took(took, thresholds)
    if took_finding:
        result["findings"].append(took_finding)

    # 3. Optional node-level search-pressure checks (need node APIs).
    if args.nodes:
        node_findings, node_err = _collect_node_findings(args.url, node_apis)
        result["findings"].extend(node_findings)
        if node_err:
            result["node_stats_error"] = node_err

    result["verdict"] = worst_severity(result["findings"])
    return result


def _collect_node_findings(base_url, node_apis):
    """Read search thread-pool + breaker stats and classify them. Returns
    (findings, error_message). Node APIs are unavailable on Serverless."""
    if not node_apis:
        return [], "endpoint exposes no node APIs (Serverless); node checks skipped"
    try:
        tp = _get(base_url, "/_nodes/stats/thread_pool?filter_path=nodes.*.name,"
                            "nodes.*.thread_pool.search")
        br = _get(base_url, "/_nodes/stats/breaker?filter_path=nodes.*.name,nodes.*.breakers")
        tp_by_name = {n.get("name", nid): n.get("thread_pool", {})
                      for nid, n in (tp.get("nodes") or {}).items()}
        br_by_name = {n.get("name", nid): n.get("breakers", {})
                      for nid, n in (br.get("nodes") or {}).items()}
        return classify_cluster(tp_by_name, br_by_name), None
    except Exception as e:  # noqa: BLE001
        return [], str(e)


def cmd_nodes(args, _thresholds):
    """Diagnose cluster-side search pressure (429 rejections, tripped breakers)
    when the complaint is load-related and there is no single slow query to
    profile. Read-only: node-stat GETs only."""
    dist, version, node_apis = detect_distribution(args.url)
    result = {"distribution": dist, "version": version, "node_apis_available": node_apis,
              "findings": []}
    node_findings, node_err = _collect_node_findings(args.url, node_apis)
    result["findings"].extend(node_findings)
    if node_err:
        result["node_stats_error"] = node_err
    result["verdict"] = worst_severity(result["findings"])
    return result


def _median_took(base_url, index, body, runs):
    """Run a search `runs` times (dropping a warmup) and return the median took."""
    body = dict(body)
    body["profile"] = False
    tooks = []
    for i in range(runs + 1):  # +1 warmup, discarded
        try:
            resp = _search(base_url, index, body)
            if i > 0 and resp.get("took") is not None:
                tooks.append(resp["took"])
        except Exception:  # noqa: BLE001
            continue
    if not tooks:
        return None, []
    tooks.sort()
    return tooks[len(tooks) // 2], tooks


def cmd_compare(args, _thresholds):
    detect_distribution(args.url)
    with open(args.before) as fh:
        before = json.load(fh)
    with open(args.after) as fh:
        after = json.load(fh)
    b_med, b_all = _median_took(args.url, args.index, before, args.runs)
    a_med, a_all = _median_took(args.url, args.index, after, args.runs)
    speedup = round(b_med / a_med, 2) if (b_med and a_med) else None
    return {
        "index": args.index, "runs": args.runs,
        "before_took_ms": b_med, "after_took_ms": a_med,
        "before_runs_ms": b_all, "after_runs_ms": a_all,
        "speedup_x": speedup,
        "verdict": ("faster" if (b_med is not None and a_med is not None and a_med <= b_med)
                    else "not-faster"),
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _print_profile(r):
    print("distribution: %s %s   index: %s   took: %s ms   verdict: %s"
          % (r["distribution"], r["version"], r["index"],
             r.get("took_ms", "?"), r["verdict"].upper()))
    if r.get("search_error"):
        print("  ! " + r["search_error"])
    if not r["findings"]:
        print("  no slow-query signals — this query looks healthy.")
        return
    for f in r["findings"]:
        where = f.get("path") or f.get("node") or f.get("subtype") or ""
        print("  [%-8s] %s%s: %s"
              % (f["severity"].upper(), f["kind"], (" @ " + where) if where else "", f["message"]))


def _print_compare(r):
    print("index: %s   runs: %d   before: %s ms   after: %s ms   speedup: %sx   verdict: %s"
          % (r["index"], r["runs"], r["before_took_ms"], r["after_took_ms"],
             r["speedup_x"], r["verdict"].upper()))


def _print_nodes(r):
    print("distribution: %s %s   node APIs: %s   verdict: %s"
          % (r["distribution"], r["version"], r["node_apis_available"], r["verdict"].upper()))
    if r.get("node_stats_error"):
        print("  ! " + r["node_stats_error"])
    if not r["findings"]:
        print("  no search-pressure signals — thread pools and breakers look healthy.")
        return
    for f in r["findings"]:
        print("  [%-8s] %s @ %s: %s"
              % (f["severity"].upper(), f["kind"], f.get("node", ""), f["message"]))


def _apply_thresholds(overrides):
    thresholds = dict(DEFAULTS)
    for kv in overrides:
        k, _, v = kv.partition("=")
        if k in thresholds:
            thresholds[k] = float(v) if ("." in v or "fraction" in k) else int(v)
    return thresholds


def main(argv=None):
    p = argparse.ArgumentParser(description="Read-only OpenSearch slow-query diagnostic")
    sub = p.add_subparsers(dest="cmd", required=True)

    pr = sub.add_parser("profile", help="profile one query and localize the slowness")
    pr.add_argument("--url", default="http://localhost:9200", help="OpenSearch endpoint")
    pr.add_argument("--index", required=True, help="index or alias to search")
    pr.add_argument("--query-file", help="JSON search body file")
    pr.add_argument("--query", help="inline JSON search body")
    pr.add_argument("--nodes", action="store_true", help="also check thread-pool/breaker pressure")
    pr.add_argument("--threshold", action="append", default=[], metavar="key=value")
    pr.add_argument("--json", action="store_true", help="emit machine-readable JSON")

    cp = sub.add_parser("compare", help="measure before/after `took` for a rewrite")
    cp.add_argument("--url", default="http://localhost:9200")
    cp.add_argument("--index", required=True)
    cp.add_argument("--before", required=True, help="the slow query body (JSON file)")
    cp.add_argument("--after", required=True, help="the rewritten query body (JSON file)")
    cp.add_argument("--runs", type=int, default=5, help="timed runs each (a warmup is discarded)")
    cp.add_argument("--json", action="store_true")

    nd = sub.add_parser("nodes", help="diagnose search thread-pool rejections / tripped breakers (no query needed)")
    nd.add_argument("--url", default="http://localhost:9200", help="OpenSearch endpoint")
    nd.add_argument("--json", action="store_true")

    args = p.parse_args(argv)
    thresholds = _apply_thresholds(getattr(args, "threshold", []))

    if args.cmd == "profile":
        result = cmd_profile(args, thresholds)
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            _print_profile(result)
        sys.exit({"fast": 0, "info": 0, "warning": 1, "critical": 2}[result["verdict"]])
    elif args.cmd == "nodes":
        result = cmd_nodes(args, thresholds)
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            _print_nodes(result)
        sys.exit({"fast": 0, "info": 0, "warning": 1, "critical": 2}[result["verdict"]])
    else:
        result = cmd_compare(args, thresholds)
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            _print_compare(result)
        sys.exit(0 if result["verdict"] == "faster" else 1)


if __name__ == "__main__":
    main()
