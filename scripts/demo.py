"""End-to-end demo against a running server (standard library only).

    python scripts/demo.py                    # 25 recipients, 2 of them invalid
    python scripts/demo.py --count 5000       # bigger job, prints throughput
    python scripts/demo.py --url http://localhost:8000 --out ./out

Submits a job, polls its progress, prints the per-row problems and downloads
the ZIP of generated certificates.
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

FIRST = [
    "Ada",
    "Alan",
    "Grace",
    "Linus",
    "Margaret",
    "Dennis",
    "Barbara",
    "Ken",
    "Radia",
    "Tim",
    "Aswin",
    "Priya",
    "Arjun",
    "Meera",
    "Karthik",
    "Divya",
    "Rahul",
    "Ananya",
]
LAST = [
    "Lovelace",
    "Turing",
    "Hopper",
    "Torvalds",
    "Hamilton",
    "Ritchie",
    "Liskov",
    "Thompson",
    "Perlman",
    "Berners-Lee",
    "Kumar",
    "Raman",
    "Iyer",
    "Nair",
    "Subramanian",
    "Krishnan",
]


def request(method: str, url: str, body: dict | None = None) -> tuple[int, bytes, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as err:
        return err.code, err.read(), dict(err.headers)


def build_payload(count: int) -> dict:
    recipients = [
        {
            "name": f"{FIRST[i % len(FIRST)]} {LAST[(i // len(FIRST)) % len(LAST)]}",
            "email": f"participant{i}@example.com",
        }
        for i in range(count)
    ]
    # A couple of bad rows to show partial validation.
    recipients.append({"name": "", "email": "missing-name@example.com"})
    recipients.append({"name": "Duplicate Person", "email": "participant0@example.com"})
    return {
        "certificate": {
            "title": "Certificate of Completion",
            "course_name": "Backend Engineering with FastAPI",
            "issuer_name": "Acme Academy",
            "description": "For completing all modules and the capstone project.",
            "signatory_name": "Priya Raman",
            "signatory_title": "Director of Learning",
        },
        "recipients": recipients,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default="http://localhost:8000")
    parser.add_argument("--count", type=int, default=23, help="number of valid recipients")
    parser.add_argument("--out", default=".", help="where to save the ZIP")
    args = parser.parse_args()
    api = f"{args.url.rstrip('/')}/api/v1"

    started = time.perf_counter()
    status, body, _ = request("POST", f"{api}/jobs", build_payload(args.count))
    accepted_in = time.perf_counter() - started
    job = json.loads(body)
    if status != 202:
        print(f"Request rejected ({status}): {json.dumps(job, indent=2)}")
        return 1

    print(
        f"Job {job['id']} accepted in {accepted_in:.2f}s "
        f"({job['progress']['total']} rows, {len(job['invalid_recipients'])} invalid)"
    )
    for row in job["invalid_recipients"]:
        problems = "; ".join(f"{e['field']}: {e['message']}" for e in row["errors"])
        print(f"  row {row['row_index']}: {problems}")

    while job["status"] in ("pending", "processing"):
        time.sleep(0.5)
        job = json.loads(request("GET", f"{api}/jobs/{job['id']}")[1])
        p = job["progress"]
        bar = "#" * int(p["percent_complete"] // 4)
        print(
            f"\r  [{bar:<25}] {p['percent_complete']:6.2f}%  generated={p['generated']} "
            f"failed={p['failed']} invalid={p['invalid']}  ({job['status']})",
            end="",
        )
    elapsed = time.perf_counter() - started
    generated = job["progress"]["generated"]
    print(
        f"\nFinished with status '{job['status']}' in {elapsed:.1f}s "
        f"(~{generated / elapsed:.0f} certificates/s end to end)"
    )

    status, content, _ = request("GET", f"{api}/jobs/{job['id']}/download")
    if status == 200:
        out = Path(args.out) / f"certificates-{job['id'][:8]}.zip"
        out.write_bytes(content)
        size_mb = len(content) / 1e6
        print(f"Saved {generated} certificates + manifest.csv to {out} ({size_mb:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
