Search on my `sqd-wild` index is slow. Here's the query — can you profile it and
tell me what to run to fix it? Endpoint is $OPENSEARCH_URL.

```json
{ "size": 10, "query": { "wildcard": { "title": { "value": "*shoe" } } } }
```
