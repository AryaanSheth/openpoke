#!/usr/bin/env python3
"""Render the load-test CSVs as PNGs for presentation.

    .venv/bin/python loadtest/plot.py

Every chart is drawn from a committed CSV in this directory, so the numbers are
reproducible rather than illustrative. Where a measurement contradicted plan.md,
the chart says so on its face — that is the point of showing them.
"""

from __future__ import annotations

import csv
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter

HERE = Path(__file__).parent
INK, MUTED, ACCENT, WARN = "#1f2933", "#7b8794", "#2f6f9f", "#c1583a"

plt.rcParams.update(
    {
        "figure.dpi": 160,
        "savefig.dpi": 160,
        "font.size": 10,
        "axes.titlesize": 12,
        "axes.titleweight": "bold",
        "axes.labelcolor": INK,
        "axes.edgecolor": MUTED,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "text.color": INK,
        "xtick.color": MUTED,
        "ytick.color": MUTED,
        "grid.color": "#e4e7eb",
        "figure.facecolor": "white",
        "axes.facecolor": "white",
    }
)


def rows(name: str) -> list[dict[str, str]]:
    """Read a results CSV, dropping incomplete samples.

    The soak sampler writes a final partial row if the database goes away
    mid-sample; plotting it would crash rather than mislead, but dropping it is
    the honest fix — it is a missing measurement, not a zero.
    """
    with (HERE / name).open() as fh:
        return [r for r in csv.DictReader(fh) if all((v or "").strip() for v in r.values())]


def thousands(x, _pos):
    if x >= 1_000_000:
        return f"{x / 1_000_000:.1f}M"
    if x >= 1_000:
        return f"{x / 1_000:.0f}k"
    return f"{x:.0f}"


def save(fig, name: str) -> None:
    out = HERE / name
    fig.tight_layout()
    fig.savefig(out, bbox_inches="tight")
    plt.close(fig)
    print(f"  wrote {out.relative_to(HERE.parent)}")


# ---------------------------------------------------------------- concurrency
def plot_concurrency() -> None:
    data = rows("results_concurrency.csv")
    clients = [int(r["clients"]) for r in data]
    jobs = [float(r["jobs_per_s"]) for r in data]
    ideal = [jobs[0] * c for c in clients]

    fig, ax = plt.subplots(figsize=(7, 4.2))
    ax.plot(clients, ideal, "--", color=MUTED, lw=1.4, label="linear scaling (claimed)")
    ax.plot(clients, jobs, "o-", color=ACCENT, lw=2.2, ms=7, label="measured")

    peak = max(range(len(jobs)), key=lambda i: jobs[i])
    ax.annotate(
        f"peak {jobs[peak] / 1000:.0f}k/s at {clients[peak]} clients",
        xy=(clients[peak], jobs[peak]),
        xytext=(clients[peak] - 5.5, jobs[peak] * 1.22),
        color=INK,
        arrowprops={"arrowstyle": "->", "color": MUTED},
    )
    ax.annotate(
        "16 clients is SLOWER than 8",
        xy=(clients[-1], jobs[-1]),
        xytext=(clients[-1] - 7, jobs[-1] * 0.55),
        color=WARN,
        fontweight="bold",
        arrowprops={"arrowstyle": "->", "color": WARN},
    )

    ax.set_xscale("log", base=2)
    ax.set_xticks(clients)
    ax.set_xticklabels(clients)
    ax.yaxis.set_major_formatter(FuncFormatter(thousands))
    ax.set_xlabel("concurrent claimers")
    ax.set_ylabel("jobs claimed / sec")
    ax.set_title("SKIP LOCKED did not scale linearly\n1→8 clients bought 2.4×, not 8×")
    ax.grid(True, ls=":", lw=0.8)
    ax.legend(frameon=False, loc="upper left")
    save(fig, "chart_concurrency.png")


# ----------------------------------------------------------------- batch size
def plot_batchsize() -> None:
    data = rows("results_batchsize.csv")
    batch = [int(r["batch"]) for r in data]
    jobs = [float(r["jobs_per_s"]) for r in data]
    commits = [float(r["commits_per_s"]) for r in data]
    x = range(len(batch))

    fig, ax = plt.subplots(figsize=(7, 4.2))
    ax2 = ax.twinx()
    ax2.spines["right"].set_visible(True)

    bars = ax.bar([i - 0.19 for i in x], jobs, width=0.38, color=ACCENT, label="jobs / sec")
    line = ax2.plot(list(x), commits, "o-", color=WARN, lw=2.2, ms=8, label="commits / sec")

    for i, v in zip(x, jobs):
        ax.text(i - 0.19, v * 1.04, thousands(v, None), ha="center", color=ACCENT, fontsize=9)
    for i, v in zip(x, commits):
        off = -0.12 if i == len(commits) - 1 else 0.12
        ha = "right" if i == len(commits) - 1 else "left"
        ax2.text(i + off, v * 1.10, thousands(v, None), ha=ha, color=WARN, fontsize=9)

    ax.set_xticks(list(x))
    ax.set_xticklabels([f"batch {b}" for b in batch])
    ax.yaxis.set_major_formatter(FuncFormatter(thousands))
    ax2.yaxis.set_major_formatter(FuncFormatter(thousands))
    ax.set_ylabel("jobs / sec", color=ACCENT)
    ax2.set_ylabel("commits / sec", color=WARN)
    ax.set_ylim(0, max(jobs) * 1.25)
    ax2.set_ylim(0, max(commits) * 1.25)
    ax.set_title("Batching is the dominant lever — but it isn't free\njobs/s ×17.5, commits/s ÷5.7")
    ax.grid(True, axis="y", ls=":", lw=0.8)
    ax.legend(handles=[bars, line[0]], frameon=False, loc="upper center")
    save(fig, "chart_batchsize.png")


# ----------------------------------------------------------------------- soak
def plot_soak() -> None:
    data = rows("results_soak.csv")
    mins = [int(r["elapsed_s"]) / 60 for r in data]
    dead = [int(r["n_dead_tup"]) for r in data]
    size = [int(r["table_bytes"]) / 1e6 for r in data]

    fig, (top, bot) = plt.subplots(
        2, 1, figsize=(7.4, 5.6), sharex=True, gridspec_kw={"height_ratios": [1.25, 1]}
    )

    top.fill_between(mins, dead, color=ACCENT, alpha=0.18)
    top.plot(mins, dead, color=ACCENT, lw=1.6)
    top.yaxis.set_major_formatter(FuncFormatter(thousands))
    top.set_ylabel("dead tuples")
    top.set_title(
        "30-minute soak: autovacuum keeps pace\n"
        "dead tuples sawtooth; table size reaches steady state and stops growing"
    )
    top.grid(True, ls=":", lw=0.8)
    top.set_ylim(0, max(dead) * 1.32)
    top.text(
        mins[1], max(dead) * 1.18,
        "each drop = autovacuum firing (27 runs over the window)",
        color=MUTED, fontsize=9,
    )

    bot.plot(mins, size, color=WARN, lw=2.2)
    flat = size[-1]
    bot.axhline(flat, ls="--", lw=1, color=MUTED)
    bot.annotate(
        f"flat at {flat:.0f} MB — space reused, not leaked",
        xy=(mins[-1], flat),
        xytext=(mins[-1] * 0.34, flat * 0.72),
        color=INK,
        arrowprops={"arrowstyle": "->", "color": MUTED},
    )
    bot.set_ylim(0, max(size) * 1.3)
    bot.set_ylabel("table size (MB)")
    bot.set_xlabel("elapsed (minutes)")
    bot.grid(True, ls=":", lw=0.8)
    save(fig, "chart_soak.png")


# ---------------------------------------------------------------- queue depth
def plot_queue_depth() -> None:
    data = rows("results_queue_depth.csv")
    t = [int(r["t"]) for r in data]
    pending = [int(r["pending"]) for r in data]
    claim = [float(r["claim_ms"]) for r in data]

    fig, ax = plt.subplots(figsize=(7, 4.2))
    ax2 = ax.twinx()
    ax2.spines["right"].set_visible(True)

    a = ax.plot(t, pending, "o-", color=ACCENT, lw=2.2, ms=5, label="queue depth (pending)")
    b = ax2.plot(t, claim, "s--", color=WARN, lw=1.6, ms=4, label="claim latency (ms)")

    ax.set_xlabel("sample")
    ax.set_ylabel("pending jobs", color=ACCENT)
    ax2.set_ylabel("claim latency (ms)", color=WARN)
    ax2.set_ylim(0, max(claim) * 1.6)
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _p: f"{int(v):,}"))
    ax.set_title(
        "Sustained overload: depth grows linearly, latency stays flat\n"
        "nothing dropped — this is the autoscaling signal"
    )
    ax.grid(True, ls=":", lw=0.8)
    ax.legend(handles=a + b, frameon=False, loc="upper left")
    save(fig, "chart_queue_depth.png")


if __name__ == "__main__":
    print("rendering charts from committed CSVs:")
    plot_concurrency()
    plot_batchsize()
    plot_soak()
    plot_queue_depth()
    print("done")
