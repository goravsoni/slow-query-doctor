#!/usr/bin/env python3
"""Eval environment: a small 'online shop' cluster for slow-query-doctor.

Resets the cluster to a clean slate, then seeds one `shop-products` index with
enough documents that the Profile API returns a meaningful breakdown and the
query anti-patterns behave as they would in production. Kept modest (a few
thousand docs) so the eval seeds fast; the larger interactive demo dataset lives
in env/seed-customer-data.py.

Stdlib only.

  python3 shop.py --url http://localhost:9200 [--docs 5000]
"""
import argparse
import json
import urllib.request
import urllib.error

MAPPING = {
    "settings": {"index": {"number_of_shards": 1, "number_of_replicas": 0}},
    "mappings": {"properties": {
        "title": {"type": "text"},
        "brand": {"type": "keyword"},
        "category": {"type": "keyword"},
        "price": {"type": "float"},
        "popularity": {"type": "integer"},
        "in_stock": {"type": "boolean"},
        "created_at": {"type": "date"},
    }},
}
ADJ = ["red", "blue", "leather", "canvas", "waterproof", "lightweight", "premium",
       "classic", "vintage", "modern", "rugged", "sleek"]
NOUN = ["shoe", "boot", "sandal", "sneaker", "loafer", "jacket", "backpack", "hat"]
CATS = ["footwear", "apparel", "accessories", "outdoor", "sale", "clearance"]


def req(base, method, path, body=None, ndjson=False):
    data, headers = None, {}
    if ndjson:
        data, headers["Content-Type"] = body.encode(), "application/x-ndjson"
    elif body is not None:
        data, headers["Content-Type"] = json.dumps(body).encode(), "application/json"
    r = urllib.request.Request(base.rstrip("/") + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(r, timeout=60) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def preflight(base):
    code, body = req(base, "GET", "/")
    if code != 200 or "number" not in json.loads(body).get("version", {}):
        raise SystemExit("cluster not reachable at %s (HTTP %s); start a cluster first" % (base, code))


def reset_cluster(base):
    code, body = req(base, "GET", "/_cat/indices?format=json&h=index")
    if code != 200:
        return
    for row in json.loads(body):
        idx = row.get("index", "")
        if not idx.startswith("."):
            req(base, "DELETE", "/" + idx)


def seed(base, n):
    preflight(base)
    reset_cluster(base)
    req(base, "PUT", "/shop-products", MAPPING)
    batch = []
    for i in range(n):
        adj, noun = ADJ[i % len(ADJ)], NOUN[(i // len(ADJ)) % len(NOUN)]
        batch.append(json.dumps({"index": {}}))
        batch.append(json.dumps({
            "title": "%s %s model %d" % (adj, noun, i),
            "brand": "brand-%04d" % (i % 1000),
            "category": CATS[i % len(CATS)],
            "price": round(5 + (i % 500) * 0.37, 2),
            "popularity": i % 1000,
            "in_stock": (i % 4 != 0),
            "created_at": "2026-%02d-%02dT00:00:00Z" % (1 + i % 12, 1 + i % 28),
        }))
        if len(batch) >= 10000:
            req(base, "POST", "/shop-products/_bulk?refresh=false", "\n".join(batch) + "\n", ndjson=True)
            batch = []
    if batch:
        req(base, "POST", "/shop-products/_bulk?refresh=false", "\n".join(batch) + "\n", ndjson=True)
    req(base, "POST", "/shop-products/_refresh")
    print("seeded environment: shop (shop-products, %d docs)" % n)


def main():
    p = argparse.ArgumentParser(description="Seed the slow-query-doctor 'shop' eval environment")
    p.add_argument("--url", default="http://localhost:9200")
    p.add_argument("--docs", type=int, default=5000)
    args = p.parse_args()
    seed(args.url, args.docs)


if __name__ == "__main__":
    main()
