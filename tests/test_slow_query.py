#!/usr/bin/env python3
"""Deterministic tests for the slow-query diagnostic's detection logic.

Exercises the pure functions against real-OpenSearch-shaped fixtures (query DSL
bodies, Profile API trees). No running cluster, no network, no `claude` — runs
anywhere python3 exists:

    python3 tests/test_slow_query.py
"""
import importlib.util
import pathlib
import unittest

_SCRIPT = (pathlib.Path(__file__).resolve().parent.parent
           / "skill" / "slow-query-doctor" / "scripts" / "slow_query.py")
_spec = importlib.util.spec_from_file_location("slow_query", _SCRIPT)
sq = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sq)


class TestExpensiveClauseScan(unittest.TestCase):
    def test_leading_wildcard_is_critical(self):  # the `wildcard-scan` scenario
        f = sq.scan_expensive_clauses({"wildcard": {"title": {"value": "*shoe"}}})
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0]["subtype"], "wildcard-leading")
        self.assertEqual(f[0]["severity"], "critical")

    def test_trailing_wildcard_is_warning(self):
        f = sq.scan_expensive_clauses({"wildcard": {"title": "shoe*"}})
        self.assertEqual(f[0]["subtype"], "wildcard")
        self.assertEqual(f[0]["severity"], "warning")

    def test_script_score_found_nested_in_bool(self):
        query = {"bool": {"must": [{"match": {"title": "shoe"}}],
                          "should": [{"script_score": {"script": {"source": "Math.log(1+doc['p'].value)"}}}]}}
        subtypes = {f["subtype"] for f in sq.scan_expensive_clauses(query)}
        self.assertIn("script_score", subtypes)

    def test_regexp_and_fuzzy(self):
        subtypes = {f["subtype"] for f in sq.scan_expensive_clauses(
            {"bool": {"must": [{"regexp": {"sku": "ab.*"}}, {"fuzzy": {"name": {"value": "shoe"}}}]}})}
        self.assertIn("regexp", subtypes)
        self.assertIn("fuzzy", subtypes)

    def test_unrounded_now_range_flagged(self):
        f = sq.scan_expensive_clauses({"range": {"@timestamp": {"gte": "now-1h"}}})
        self.assertEqual(f[0]["subtype"], "range-unrounded-now")

    def test_rounded_now_range_not_flagged(self):
        self.assertEqual(sq.scan_expensive_clauses({"range": {"@timestamp": {"gte": "now-1h/h"}}}), [])

    def test_clean_query_has_no_findings(self):  # the `clean-fast-query-pass` scenario
        self.assertEqual(sq.scan_expensive_clauses(
            {"bool": {"must": [{"match": {"title": "running shoe"}}],
                      "filter": [{"term": {"in_stock": True}}]}}), [])


class TestPagination(unittest.TestCase):
    def test_over_window_is_critical(self):  # the `deep-pagination` scenario
        f = sq.classify_pagination(50000, 100)
        self.assertEqual(f["severity"], "critical")
        self.assertIn("search_after", f["message"])

    def test_deep_from_is_warning(self):
        self.assertEqual(sq.classify_pagination(2000, 20)["severity"], "warning")

    def test_shallow_is_healthy(self):
        self.assertIsNone(sq.classify_pagination(0, 10))
        self.assertIsNone(sq.classify_pagination(None, None))


class TestFetch(unittest.TestCase):
    def test_aggs_without_size_zero_flagged(self):  # the `agg-no-size` scenario
        f = sq.classify_fetch(size=None, has_aggs=True)
        self.assertEqual(f["kind"], "fetch-without-size")
        self.assertIn("size", f)

    def test_aggs_with_size_zero_is_clean(self):
        self.assertIsNone(sq.classify_fetch(size=0, has_aggs=True))

    def test_no_aggs_is_clean(self):
        self.assertIsNone(sq.classify_fetch(size=10, has_aggs=False))


def _qnode(typ, nanos, children=None):
    return {"type": typ, "time_in_nanos": nanos, "children": children or []}


class TestProfileSummary(unittest.TestCase):
    def test_self_time_buckets_by_type_no_double_count(self):
        # A BooleanQuery (100) wrapping a WildcardQuery (90); self-time of the
        # bool is 10, wildcard 90 — wildcard must be the hotspot.
        profile = {"shards": [{"searches": [{
            "query": [_qnode("BooleanQuery", 100, [_qnode("WildcardQuery", 90)])],
            "rewrite_time": 5,
            "collector": [{"name": "SimpleTopScoreDocCollector", "time_in_nanos": 8}],
        }], "aggregations": []}]}
        s = sq.summarize_profile(profile)
        self.assertEqual(s["hotspot_query_type"], "WildcardQuery")
        self.assertEqual(s["query_ns"], 100)
        self.assertEqual(s["rewrite_ns"], 5)

    def test_aggregation_time_summed(self):
        profile = {"shards": [{"searches": [{"query": [_qnode("MatchAllDocsQuery", 10)]}],
                               "aggregations": [{"type": "GlobalOrdinalsStringTermsAggregator",
                                                 "time_in_nanos": 500, "children": []}]}]}
        s = sq.summarize_profile(profile)
        self.assertEqual(s["aggregation_ns"], 500)


class TestHotspot(unittest.TestCase):
    def test_aggregations_dominate(self):
        summary = {"query_ns": 100, "aggregation_ns": 900, "fetch_ns": 0,
                   "hotspot_query_type": "MatchAllDocsQuery"}
        kinds = {(f["kind"], f["subtype"]) for f in sq.classify_hotspot(summary)}
        self.assertIn(("profile-hotspot", "aggregations"), kinds)

    def test_query_dominates_names_type(self):
        summary = {"query_ns": 950, "aggregation_ns": 50, "fetch_ns": 0,
                   "hotspot_query_type": "WildcardQuery"}
        q = next(f for f in sq.classify_hotspot(summary) if f["subtype"] == "query")
        self.assertEqual(q["dominant_type"], "WildcardQuery")

    def test_no_hotspot_when_balanced(self):
        summary = {"query_ns": 34, "aggregation_ns": 33, "fetch_ns": 33,
                   "hotspot_query_type": "BooleanQuery"}
        self.assertEqual(sq.classify_hotspot(summary), [])


class TestTook(unittest.TestCase):
    def test_took_thresholds(self):
        self.assertIsNone(sq.classify_took(120))
        self.assertEqual(sq.classify_took(800)["severity"], "warning")
        self.assertEqual(sq.classify_took(5000)["severity"], "critical")


class TestClusterPressure(unittest.TestCase):
    def test_search_rejections_critical(self):
        f = sq.classify_cluster({"node-1": {"search": {"rejected": 12, "queue": 1000}}}, {})
        self.assertEqual(f[0]["kind"], "thread-pool-rejections")
        self.assertEqual(f[0]["severity"], "critical")

    def test_breaker_tripped_critical(self):
        f = sq.classify_cluster({}, {"node-1": {"fielddata": {"tripped": 3}}})
        self.assertEqual(f[0]["kind"], "circuit-breaker-tripped")
        self.assertEqual(f[0]["breaker"], "fielddata")

    def test_healthy_nodes_no_findings(self):
        self.assertEqual(sq.classify_cluster({"n": {"search": {"rejected": 0, "queue": 0}}},
                                             {"n": {"request": {"tripped": 0}}}), [])


class TestVerdict(unittest.TestCase):
    def test_worst(self):
        self.assertEqual(sq.worst_severity([]), "fast")
        self.assertEqual(sq.worst_severity([{"severity": "warning"}, {"severity": "critical"}]),
                         "critical")


if __name__ == "__main__":
    unittest.main(verbosity=2)
