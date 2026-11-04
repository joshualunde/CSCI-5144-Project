#!/usr/bin/env python3
"""Plot *measured* Locust p95 / RPS and Kubernetes replica counts.

Example:
 python plot_results.py --locust-prefix results/scheduled_hpa --replicas results/hpa_replicas.csv --out-dir figures

Requires: pip install pandas matplotlib
"""
import argparse
from pathlib import Path
import pandas as pd
import matplotlib.pyplot as plt


def load_csv(path):
    if not path.exists():
        raise SystemExit(f"Missing data: {path}")
    return pd.read_csv(path)


def find_aggregate(frame):
    if "Name" not in frame.columns:
        raise SystemExit(f"Expected Locust 'Name' column; found {list(frame.columns)}")
    names = frame["Name"].astype(str).str.strip().str.lower()
    agg = frame[names.isin(["aggregated", "aggregate", "total"])].copy()
    if agg.empty:
        raise SystemExit("Could not find Locust aggregate row. Inspect Name column in your CSV.")
    return agg


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--locust-prefix", default="results/scheduled_hpa")
    ap.add_argument("--replicas", default="results/hpa_replicas.csv")
    ap.add_argument("--out-dir", default="figures")
    args = ap.parse_args()
    prefix = Path(args.locust_prefix)
    stats = find_aggregate(load_csv(Path(str(prefix) + "_stats.csv")))
    hist = find_aggregate(load_csv(Path(str(prefix) + "_stats_history.csv")))
    replicas = load_csv(Path(args.replicas))
    output = Path(args.out_dir)
    output.mkdir(parents=True, exist_ok=True)
    if "95%" not in stats or "95%" not in hist:
        raise SystemExit("Locust CSV lacks the 95% percentile column. Check Locust version/output.")
    if "Timestamp" not in hist or "unix_ts" not in replicas:
        raise SystemExit("Missing timestamp fields. Use provided record_replicas.py and Locust --csv.")
    for col in ("Timestamp", "95%", "Requests/s"):
        hist[col] = pd.to_numeric(hist[col], errors="coerce")
    for col in ("unix_ts", "deployment_ready", "deployment_desired", "hpa_desired"):
        if col in replicas:
            replicas[col] = pd.to_numeric(replicas[col], errors="coerce")
    hist = hist.dropna(subset=["Timestamp", "95%"])
    replicas = replicas.dropna(subset=["unix_ts"])
    if hist.empty or replicas.empty:
        raise SystemExit("No timestamped data available; run real experiments first.")
    start = min(hist["Timestamp"].min(), replicas["unix_ts"].min())
    hist["minute"] = (hist["Timestamp"] - start) / 60
    replicas["minute"] = (replicas["unix_ts"] - start) / 60

    plt.rcParams.update({"font.size": 11})
    fig, ax = plt.subplots(figsize=(9, 4.7))
    ax.plot(hist["minute"], hist["95%"], lw=2, label="Locust p95")
    ax.set(xlabel="Minutes from monitoring start", ylabel="p95 latency (ms)", title="Measured p95 HTTP response latency over time")
    ax.grid(alpha=.2);ax.legend();fig.tight_layout()
    fig.savefig(output/"p95_latency.png", dpi=180)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(9, 4.7))
    ax.step(replicas["minute"], replicas["deployment_ready"], where="post", label="Ready pods", lw=2)
    if "hpa_desired" in replicas and replicas["hpa_desired"].notna().any():
        ax.step(replicas["minute"], replicas["hpa_desired"], where="post", label="HPA desired", lw=1.8)
    ax.set(xlabel="Minutes from monitoring start", ylabel="Replicas", title="Measured Kubernetes pod scaling")
    ax.grid(alpha=.2);ax.legend();fig.tight_layout()
    fig.savefig(output/"replicas_over_time.png", dpi=180)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(9, 4.7))
    ax.plot(hist["minute"], hist["95%"], label="p95 latency (ms)", lw=2)
    ax.set(xlabel="Minutes from monitoring start", ylabel="p95 latency (ms)")
    ax2 = ax.twinx()
    ax2.plot(hist["minute"], hist["Requests/s"], label="Requests/s", lw=1.5, alpha=.75, linestyle="--")
    ax2.set_ylabel("Requests/s")
    ax.set_title("Measured demand and latency")
    ax.grid(alpha=.2);fig.tight_layout()
    fig.savefig(output/"rps_vs_latency.png", dpi=180)
    plt.close(fig)

    # Whole-run p95 is read from the final stats aggregate, NOT averaged from history.
    p95 = pd.to_numeric(stats.iloc[-1]["95%"], errors="coerce")
    requests = stats.iloc[-1].get("Request Count", "?")
    failures = stats.iloc[-1].get("Failure Count", "?")
    print(f"Whole-run p95: {p95} ms | Requests: {requests} | Failures: {failures}")
    print("Saved: p95_latency.png, replicas_over_time.png, rps_vs_latency.png")
    print("NOTE: History percentiles are sliding-window estimates; do NOT average them to get whole-run p95.")


if __name__ == "__main__":
    main()
