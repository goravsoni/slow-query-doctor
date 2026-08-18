This search against my `sqd-deep` index is really slow and sometimes errors out.
Can you profile it and tell me exactly how to make it fast? The cluster is at
$OPENSEARCH_URL.

```json
{ "from": 50000, "size": 100, "query": { "match_all": {} } }
```
