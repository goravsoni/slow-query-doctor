#!/usr/bin/env python3
"""Seed a realistic 'online shop' cluster for the slow-query-doctor demo.

Creates one index (`shop-products`) with enough documents that the classic
query anti-patterns are *measurably* slow, so `compare` shows a real before/after
delta on camera:

  - a leading-wildcard search vs a match on an analyzed field,
  - a script_score vs a function_score field_value_factor,
  - deep from/size pagination vs search_after,
  - a terms aggregation without size:0 vs with it.

Stdlib only (no third-party packages). Idempotent: recreates the index each run.

  python3 env/seed-customer-data.py [--url http://localhost:9200] [--docs 50000]
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

ADJ = ["red", "blue", "green", "leather", "canvas", "waterproof", "lightweight",
       "premium", "classic", "vintage", "modern", "rugged", "sleek", "cozy"]
NOUN = ["shoe", "boot", "sandal", "sneaker", "loafer", "slipper", "cleat", "heel",
        "jacket", "backpack", "hat", "glove", "sock", "belt"]
CATS = ["footwear", "apparel", "accessories", "outdoor", "sale", "clearance",
        "new-arrivals", "bestsellers"]


def _req(base, method, path, data=None):
    r = urllib.request.Request(base.rstrip("/") + path, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    try:
        return urllib.request.urlopen(r, timeout=120).read()
    except urllib.error.HTTPError as e:
        if not (method == "DELETE" and e.code == 404):
            print("  ! %s %s -> HTTP %d" % (method, path, e.code))


def seed(base, n):
    _req(base, "DELETE", "/shop-products")
    _req(base, "PUT", "/shop-products", json.dumps(MAPPING).encode())
    batch, sent = [], 0
    for i in range(n):
        adj, noun = ADJ[i % len(ADJ)], NOUN[(i // len(ADJ)) % len(NOUN)]
        batch.append(json.dumps({"index": {"_index": "shop-products"}}))
        batch.append(json.dumps({
            "title": "%s %s model %d" % (adj, noun, i),
            "brand": "brand-%04d" % (i % 2000),      # high cardinality
            "category": CATS[i % len(CATS)],
            "price": round(5 + (i % 500) * 0.37, 2),
            "popularity": i % 1000,
            "in_stock": (i % 4 != 0),
            "created_at": "2026-%02d-%02dT00:00:00Z" % (1 + i % 12, 1 + i % 28),
        }))
        if len(batch) >= 10000:
            _req(base, "POST", "/_bulk", ("\n".join(batch) + "\n").encode())
            sent += len(batch) // 2
            batch = []
            print("  ... indexed %d/%d" % (sent, n))
    if batch:
        _req(base, "POST", "/_bulk", ("\n".join(batch) + "\n").encode())
        sent += len(batch) // 2
    _req(base, "POST", "/shop-products/_refresh")
    print("seeded shop-products: %d docs on %s" % (sent, base))


def main():
    p = argparse.ArgumentParser(description="Seed the slow-query-doctor demo cluster")
    p.add_argument("--url", default="http://localhost:9200")
    p.add_argument("--docs", type=int, default=50000)
    args = p.parse_args()
    seed(args.url, args.docs)


if __name__ == "__main__":
    main()
