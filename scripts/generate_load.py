#!/usr/bin/env python3
"""
Generate traffic to hello-world.example.com to test:
  - Rate limiting (expect 429 after burst exceeded)
  - Circuit breaker (misconfigure backend port → backend returns 503; CB trips at 25% error rate).
    Configure the circuit breaker with response_code=429 (or 504) so when the CB is open you get
    that code instead of 503—then you can tell "backend 503" from "circuit breaker open (429)".

Usage:
  # Test rate limit (default: 200 requests as fast as possible)
  python scripts/test_traffic_middleware.py

  # Custom URL (e.g. via port-forward or /etc/hosts)# Rate limit test (200 requests as fast as possible; expect 200 then 429)
python scripts/test_traffic_middleware.py 

# Fewer requests
python scripts/test_traffic_middleware.py  -n 100

# Slower (stay under limit, mostly 200)
python scripts/test_traffic_middleware.py --url http://hello-world-green.example.com:31888 -n 50

  # Fewer/more requests
  python scripts/test_traffic_middleware.py --url http://hello-world.example.com:32594 -n 250

  # Slower (e.g. stay under rate limit to see 200s only)
  python scripts/test_traffic_middleware.py --url http://hello-world.example.com:32594  --delay 0.1
"""

import argparse
import time
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

DEFAULT_URL = "http://hello-world.example.com"
DEFAULT_REQUESTS = 200


def main():
    p = argparse.ArgumentParser(description="Generate traffic to test rate limit and circuit breaker")
    p.add_argument("--url", default=DEFAULT_URL, help=f"Base URL (default: {DEFAULT_URL})")
    p.add_argument("--host", default=None, help="Host header (default: from URL). Use for port-forward: --url http://localhost:8080 --host hello-world.example.com")
    p.add_argument("-n", "--num", type=int, default=DEFAULT_REQUESTS, help="Number of requests")
    p.add_argument("--delay", type=float, default=0, help="Delay between requests in seconds (0 = as fast as possible)")
    p.add_argument("--path", default="/", help="Path to request (default: /)")
    args = p.parse_args()

    url = args.url.rstrip("/") + args.path
    # Host header: explicit --host, or from URL if it's hello-world.example.com, else derived from URL
    if args.host:
        host = args.host
    elif "hello-world.example.com" in args.url:
        host = "hello-world.example.com"
    else:
        try:
            from urllib.parse import urlparse
            host = urlparse(args.url).netloc.split(":")[0]
        except Exception:
            host = None

    print(f"Target: {url}")
    print(f"Requests: {args.num} (delay: {args.delay}s)")
    if host:
        print(f"Host header: {host}")
    print("-" * 60)

    stats = {"200": 0, "429": 0, "503": 0, "other": 0, "error": 0}
    start = time.perf_counter()

    for i in range(1, args.num + 1):
        req = Request(url, method="GET")
        if host:
            req.add_header("Host", host)
        t0 = time.perf_counter()
        try:
            r = urlopen(req, timeout=10)
            elapsed = (time.perf_counter() - t0) * 1000
            code = r.getcode()
            body_len = len(r.read())
            r.close()
            status_key = str(code)
            if status_key not in stats:
                stats[status_key] = 0
            stats[status_key] = stats.get(status_key, 0) + 1
            print(f"  #{i:4d}  GET {url}  ->  {code}  (body: {body_len} bytes, {elapsed:.0f} ms)")
        except HTTPError as e:
            elapsed = (time.perf_counter() - t0) * 1000
            code = e.code
            status_key = str(code)
            stats[status_key] = stats.get(status_key, 0) + 1
            try:
                body = e.read().decode("utf-8", errors="replace")[:200]
            except Exception:
                body = "(no body)"
            print(f"  #{i:4d}  GET {url}  ->  {code}  (body: {repr(body)[:80]}..., {elapsed:.0f} ms)")
        except URLError as e:
            stats["error"] += 1
            print(f"  #{i:4d}  GET {url}  ->  ERROR: {e.reason}")
        except Exception as e:
            stats["error"] += 1
            print(f"  #{i:4d}  GET {url}  ->  ERROR: {e}")

        if args.delay > 0:
            time.sleep(args.delay)

    total = time.perf_counter() - start
    print("-" * 60)
    print("Summary:")
    for k in sorted(stats.keys()):
        print(f"  {k}: {stats[k]}")
    print(f"  Total time: {total:.2f}s  ({args.num / total:.1f} req/s)")


if __name__ == "__main__":
    main()