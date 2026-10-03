"""批量导出全项目管网台账。

一次抓取：用整组锚点属性把所有带管网普查数据的图元筛出来（不同普查单位的
字段集不同，单一锚点必漏），再按实体类型分成管线表和检查井表。

    python batch_pipes.py <图纸目录> <产物目录>

「小区」取图纸目录下的第一层子目录名。每张图独立进程，单张失败不影响整体；进度实时写盘，
中断后重跑跳过已完成的。
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import acad

MAX_ENTITIES = 80000

# 线状图元是管线，点状图元是井/雨水口/排放口
PIPE_TYPES = {"LINE", "LWPOLYLINE", "POLYLINE", "ARC", "SPLINE"}
WELL_TYPES = {"INSERT", "POINT", "CIRCLE"}

# 在 ssget 层就滤掉标注文字：一张 50 万图元的地形图里带属性的 TEXT 有好几万，
# 全导出会把中间文本撑到几百 MB，而管网台账要的是管和井。
ENTITY_TYPES = ",".join(sorted(PIPE_TYPES | WELL_TYPES))

SKIP = ("封面", "目录", "说明", "区位", "大样", "图例")


def targets(base: Path) -> list[Path]:
    return [p for p in sorted(base.rglob("*.dwg")) if not any(s in p.name for s in SKIP)]


def project_of(p: Path, base: Path) -> str:
    rel = p.relative_to(base)
    return rel.parts[0] if len(rel.parts) > 1 else "(根目录)"


def write_csv(path: Path, rows: list[dict]) -> None:
    cols: list[str] = ["_小区", "_图纸"]
    for r in rows:
        for k in r:
            if k not in cols:
                cols.append(k)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for r in rows:
            w.writerow([r.get(c, "") for c in cols])


def main() -> int:
    ap = argparse.ArgumentParser(description="批量导出管网台账")
    ap.add_argument("raw", type=Path, help="图纸目录")
    ap.add_argument("out", type=Path, help="产物目录")
    a = ap.parse_args()
    base, OUT = a.raw.resolve(), a.out.resolve()
    OUT.mkdir(parents=True, exist_ok=True)
    files = targets(base)
    state_file = OUT / "_progress2.json"
    state = json.loads(state_file.read_text(encoding="utf-8")) if state_file.exists() else {}

    print(f"待处理 {len(files)} 张图", flush=True)
    print(f"锚点 {len(acad.ALL_ANCHORS)} 个，上限 {MAX_ENTITIES}", flush=True)
    t0 = time.time()
    pipes: list[dict] = []
    wells: list[dict] = []
    dropped = 0

    for idx, dwg in enumerate(files, 1):
        proj = project_of(dwg, base)
        cache = OUT / f"{proj}__{dwg.stem}__all.json"

        if str(dwg) in state and cache.exists():
            rows = json.loads(cache.read_text(encoding="utf-8"))
            print(f"[{idx}/{len(files)}] 跳过(已完成) {dwg.name}", flush=True)
        else:
            try:
                res, rows, matched = acad.extract_xdata(
                    dwg, acad.ALL_ANCHORS, types=ENTITY_TYPES,
                    max_entities=MAX_ENTITIES, timeout=1500,
                )
            except Exception as exc:
                print(f"[{idx}/{len(files)}] {dwg.name}  异常: {exc}", flush=True)
                state[str(dwg)] = {"图纸": dwg.name, "小区": proj, "错误": str(exc)[:120]}
                state_file.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
                continue
            if not res.ok:
                print(f"[{idx}/{len(files)}] {dwg.name}  失败: {res.error[:70]}", flush=True)
                state[str(dwg)] = {"图纸": dwg.name, "小区": proj, "错误": res.error[:120]}
                state_file.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
                continue
            if rows:
                cache.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
            npipe = sum(1 for r in rows if r.get("类型") in PIPE_TYPES)
            nwell = sum(1 for r in rows if r.get("类型") in WELL_TYPES)
            state[str(dwg)] = {
                "图纸": dwg.name, "小区": proj, "匹配": matched,
                "导出": len(rows), "管线": npipe, "井": nwell,
                "截断": matched > len(rows),
            }
            state_file.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
            flag = "  [截断]" if matched > len(rows) else ""
            print(f"[{idx}/{len(files)}] {proj} / {dwg.name}  "
                  f"管={npipe} 井={nwell} (匹配{matched}){flag}", flush=True)

        for r in rows:
            t = r.get("类型")
            if t not in PIPE_TYPES and t not in WELL_TYPES:
                # 早期缓存是在 ssget 加类型过滤之前跑的，里面混了带属性的 TEXT 标注。
                # 只有部分图纸有，凑一张表口径不齐，不如丢掉；要标注用 read_drawing(mode='text')。
                dropped += 1
                continue
            r["_小区"] = proj
            r["_图纸"] = dwg.name
            (pipes if t in PIPE_TYPES else wells).append(r)

    for name, rows in (("管线", pipes), ("检查井", wells)):
        if rows:
            p = OUT / f"全项目-{name}.csv"
            write_csv(p, rows)
            print(f"\n汇总 {name}: {len(rows)} 行 -> {p.name}", flush=True)
    if dropped:
        print(f"（丢弃旧缓存里的 {dropped} 条标注文字）", flush=True)

    print(f"\n总耗时 {time.time() - t0:.0f}s  完成于 {datetime.now():%H:%M:%S}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
