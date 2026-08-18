I only need the category breakdown from my `sqd-agg` index, but this query feels
heavier than it should be. Profile it and tell me how to make it cheaper.
Cluster: $OPENSEARCH_URL.

```json
{ "query": { "match_all": {} }, "aggs": { "by_category": { "terms": { "field": "category" } } } }
```
