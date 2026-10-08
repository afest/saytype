"""T-487: сводка парного бенчмарка по JSONL из tools/bench_ui.py.

    python tools/bench_report.py [_dev/bench/results] [--md summary.md]

Группирует прогоны (без warmup) по (device, mode, sample, visible), считает
n / медиану / мин / макс / p25–p75 для wall_sec и elapsed_sec у каждого UI,
и разницу медиан v5 относительно legacy в процентах. p95 на выборке из 10
не считаем — он там нестабилен. Отдельно — idle и отзывчивость GUI.
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np


def load(dirpath: Path) -> list[dict]:
    rows = []
    for p in sorted(dirpath.glob("*.jsonl")):
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
    return rows


def q(vals, p):
    return float(np.percentile(np.array(vals, dtype=float), p))


def stat(vals: list[float]) -> dict:
    if not vals:
        return {"n": 0}
    return {"n": len(vals), "med": q(vals, 50), "min": min(vals), "max": max(vals),
            "p25": q(vals, 25), "p75": q(vals, 75)}


def fmt(s: dict, key="med") -> str:
    if not s.get("n"):
        return "—"
    return f"{s['med']:.2f} ({s['min']:.2f}–{s['max']:.2f}, n={s['n']})"


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dirpath = Path(args[0]) if args else Path("_dev/bench/results")
    md_out = None
    if "--md" in sys.argv:
        md_out = Path(sys.argv[sys.argv.index("--md") + 1])
    rows = load(dirpath)
    runs = [r for r in rows if r.get("kind") != "idle" and not r.get("warmup")]
    idles = [r for r in rows if r.get("kind") == "idle"]

    cells = defaultdict(lambda: defaultdict(list))  # (device,mode,sample,visible) -> ui -> rows
    for r in runs:
        cells[(r.get("surface", "screen"), r["device"], r["mode"], r["sample"], bool(r["visible"]))][r["ui"]].append(r)

    out = []
    out.append("| поверхность | device | mode | sample | окно | метрика | legacy: медиана (мин–макс, n) | v5: медиана (мин–макс, n) | Δ медиан v5 vs legacy |")
    out.append("|---|---|---|---|---|---|---|---|---|")
    for key in sorted(cells):
        surface, device, mode, sample, vis = key
        by_ui = cells[key]
        for metric, label in (("elapsed_sec", "transcribe, с"), ("wall_sec", "финализация, с"),
                              ("proc_cpu_sec", "CPU процесса, с"), ("lag_p95_ms", "GUI lag p95, мс"),
                              ("lag_max_ms", "GUI lag max, мс")):
            leg = stat([r[metric] for r in by_ui.get("legacy", []) if r.get(metric) is not None])
            v5 = stat([r[metric] for r in by_ui.get("v5", []) if r.get(metric) is not None])
            delta = "—"
            if leg.get("n") and v5.get("n") and leg["med"]:
                d = (v5["med"] - leg["med"]) / leg["med"] * 100
                delta = f"{d:+.1f}%"
            out.append(f"| {surface} | {device} | {mode} | {sample} | {'видно' if vis else 'скрыто'} | {label} | {fmt(leg)} | {fmt(v5)} | {delta} |")
    out.append("")
    out.append("Idle (CPU процесса в % одного ядра за период покоя; GUI lag — задержка таймера 50 мс):")
    out.append("")
    out.append("| ui | поверхность | device | окно | CPU %, 1 ядро | sys busy % | RSS МБ | lag p95 мс | lag max мс |")
    out.append("|---|---|---|---|---|---|---|---|---|")
    for r in sorted(idles, key=lambda r: (r["ui"], r["device"], not r["visible"])):
        out.append(f"| {r['ui']} | {r.get('surface', 'screen')} | {r['device']} | {'видно' if r['visible'] else 'скрыто'} | {r['proc_cpu_pct_one_core']} | {r['sys_busy_pct']} | {r['rss_mb']} | {r['lag_p95_ms']} | {r['lag_max_ms']} |")
    out.append("")
    # чужая нагрузка: прогоны, где рядом был другой GPU-процесс или sys busy высокий
    def _foreign(r):
        gpu = r.get("gpu_before") or {}
        util = gpu.get("util")
        return ((r.get("sys_busy_pct") or 0) > 85) or (util is not None and util > 50)
    flagged = [r for r in runs if _foreign(r)]
    out.append(f"Прогонов с посторонней нагрузкой (система занята >85 % или GPU >50 % до старта): {len(flagged)} из {len(runs)}.")
    tags = sorted({r["tag"] for r in runs})
    out.append("Файлы прогонов: " + ", ".join(tags))
    text = "\n".join(out)
    print(text)
    if md_out:
        md_out.write_text(text + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
