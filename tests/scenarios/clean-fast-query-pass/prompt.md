Can you profile this query against my `sqd-clean` index and confirm whether it's
well-formed for performance? I want to be sure before we scale up. Endpoint:
$OPENSEARCH_URL.

```json
{
  "size": 10,
  "_source": ["title", "price"],
  "query": { "bool": {
    "must": [ { "match": { "title": "shoe" } } ],
    "filter": [ { "term": { "in_stock": true } } ]
  } }
}
```
