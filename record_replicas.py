#!/usr/bin/env python3
"""Sample a Kubernetes Deployment and HPA into timestamped CSV.

Example:
 python record_replicas.py --namespace default --deployment exchange-api --hpa exchange-hpa --output results/hpa_replicas.csv

Run BEFORE starting Locust, Ctrl+C after test ends. Requires kubectl and access to a cluster.
"""
import argparse
import csv
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


def get_json(namespace, kind, name):
    command = ["kubectl", "-n", namespace, "get", kind, name, "-o", "json"]
    result = subprocess.run(command, capture_output=True, text=True, timeout=12)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or f"kubectl failed: {kind}/{name}")
    return json.loads(result.stdout)


def main():
    p = argparse.ArgumentParser(description="Record real Kubernetes Deployment/HPA replicas every few seconds")
    p.add_argument("--namespace", default="default")
    p.add_argument("--deployment", default="exchange-api")
    p.add_argument("--hpa", default="exchange-hpa")
    p.add_argument("--interval", type=float, default=5.0)
    p.add_argument("--output", default="results/hpa_replicas.csv")
    args = p.parse_args()
    if args.interval < 1:
        p.error("--interval must be at least 1 second")
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    print(f"Recording Deployment/{args.deployment} and HPA/{args.hpa} in {args.namespace} to {path}")
    print("Start this process BEFORE Locust; stop it with Ctrl+C after the test.")
    # Verify both objects before creating an output that might be mistaken for valid data.
    for kind, name in (("deployment", args.deployment), ("hpa", args.hpa)):
        try:
            get_json(args.namespace, kind, name)
        except Exception as exc:
            sys.exit(f"ERROR: Could not read {kind}/{name}: {exc}")
    started = time.monotonic()
    fields = ["timestamp_utc", "unix_ts", "elapsed_s", "deployment_desired", "deployment_ready", "hpa_current", "hpa_desired", "error"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        while True:
            begin = time.monotonic()
            now = datetime.now(timezone.utc)
            row = {"timestamp_utc": now.isoformat(timespec="milliseconds"), "unix_ts": f"{now.timestamp():.3f}", "elapsed_s": f"{begin-started:.2f}", "error": ""}
            try:
                dep = get_json(args.namespace, "deployment", args.deployment)
                hpa = get_json(args.namespace, "hpa", args.hpa)
                row.update({
                    "deployment_desired": dep.get("spec", {}).get("replicas", ""),
                    "deployment_ready": dep.get("status", {}).get("readyReplicas", 0),
                    "hpa_current": hpa.get("status", {}).get("currentReplicas", ""),
                    "hpa_desired": hpa.get("status", {}).get("desiredReplicas", ""),
                })
            except Exception as exc:
                row["error"] = str(exc)[:180]
                print("Sampling error:", exc, file=sys.stderr)
            writer.writerow(row)
            handle.flush()
            print(f"{row['timestamp_utc']} ready={row.get('deployment_ready','?')} desired={row.get('deployment_desired','?')} hpa={row.get('hpa_desired','?')}")
            try:
                time.sleep(max(0, args.interval-(time.monotonic()-begin)))
            except KeyboardInterrupt:
                break
    print(f"Saved {path}")


if __name__ == '__main__':
    main()
