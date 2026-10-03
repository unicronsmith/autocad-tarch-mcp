"""金标准评测：图里内嵌的设计院工程量表 对 从图上量出来的数量。

    python eval_gold.py <RAW目录> <产物目录> --gold <金标准文件>

金标准文件按项目自己写，格式见 gold/示例.json。答案不用另外准备：设计院把工程量表（Excel）嵌在布局里，
从 OLE2FRAME 里逐格取出来就是。金标准文件（gold/*.json）只写两样：评哪几张图、每个表项在图上怎么量
（数某图层上的某个块，或量某图层的线长）。规则要写「依据」—— 图例、块属性、图层名 —— 不许看着数量去凑。

表里每一行都有交代：有规则的量出来对数，相符 / 不符；没有规则的记「无规则」。几行共用一个符号时
（壁挂式、落地式配电箱都画成 PDXF）合成一个核对项，对的是数量之和。同一份表在一张图里嵌了两次只算一次。

量法是最朴素的一种：模型空间里全数。一张图里带着别的小区的内容、或者一处画了两遍，就会数多 ——
这正是评测要暴露的，不在这里修。

产物：评测明细.csv（一行一个表项）、评测汇总.json。
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import math
import sys
import time
import traceback
import uuid
from collections import Counter, defaultdict
from pathlib import Path

import ezdxf
import olefile
import openpyxl

sys.path.insert(0, str(Path(__file__).resolve().parent))
import acad

# 表里的长度按整米填，图上量出来带小数
LENGTH_TOL = 1.0
NAME_HEADS = ("名称", "项目名称")
OLE_MAGIC = bytes.fromhex("D0CF11E0A1B11AE1")
COLUMNS = ("图纸", "分表", "名称", "规格", "单位", "数量", "结论", "图上数量", "核对项", "核对项设计合计", "依据")


def load(dwg: Path):
    dxf = acad.WORK / f"eval-{uuid.uuid4().hex[:12]}.dxf"
    try:
        return ezdxf.readfile(acad.dxfout(dwg, dxf))
    finally:
        dxf.unlink(missing_ok=True)


def embedded_tables(doc) -> tuple[list[list[list]], int]:
    """各布局里内嵌的 Excel，每份取活动工作表的全部行，内容相同的只留一份；外加 OLE 对象总数。"""
    tables, frames = [], 0
    for layout in doc.layouts:
        for e in layout.query("OLE2FRAME"):
            frames += 1
            raw = e.binary_data()
            # 前面是 AutoCAD 自己的头（实测 128 字节），复合文档从魔数开始
            at = raw.find(OLE_MAGIC)
            if at < 0:
                continue
            ole = olefile.OleFileIO(io.BytesIO(raw[at:]))
            if not ole.exists("Package"):
                continue
            book = openpyxl.load_workbook(io.BytesIO(ole.openstream("Package").read()), data_only=True)
            rows = [list(r) for r in book.active.iter_rows(values_only=True)]
            if rows not in tables:
                tables.append(rows)
    return tables, frames


def number(v) -> float | None:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    try:
        return float(str(v).strip())
    except ValueError:
        return None


def table_items(rows: list[list]) -> list[dict]:
    """表头行（有「数量」和「名称」或「项目名称」）定列，往下数量是数的行算一个表项；一份表里可以有几段表头。"""
    items, cols, title = [], None, ""
    for row in rows:
        cells = ["" if c is None else str(c).strip() for c in row]
        head = next((h for h in NAME_HEADS if h in cells), None)
        if head and "数量" in cells:
            cols = {"名称": cells.index(head), "数量": cells.index("数量"),
                    "单位": cells.index("单位") if "单位" in cells else None,
                    "规格": next((i for i, c in enumerate(cells) if c.startswith("规格")), None)}
        elif cells and cells[0] and not any(cells[1:]):
            title = cells[0]
        elif cols and cells[cols["名称"]] and number(row[cols["数量"]]) is not None:
            items.append({"分表": title, "名称": cells[cols["名称"]],
                          "规格": cells[cols["规格"]] if cols["规格"] is not None else "",
                          "单位": cells[cols["单位"]] if cols["单位"] is not None else "",
                          "数量": number(row[cols["数量"]])})
    return items


def path_length(points: list[tuple[float, float, float]], closed: bool) -> float:
    if closed and points:
        points = points + points[:1]
    total = 0.0
    for (x1, y1, bulge), (x2, y2, _) in zip(points, points[1:]):
        chord = math.hypot(x2 - x1, y2 - y1)
        if bulge and chord:
            angle = 4 * math.atan(abs(bulge))
            total += chord * angle / (2 * math.sin(angle / 2))
        else:
            total += chord
    return total


def measure(doc) -> tuple[Counter, dict[str, float]]:
    """模型空间里 (图层, 块名) 的个数、各图层直线和二维多段线的总长。"""
    blocks, lengths = Counter(), defaultdict(float)
    for e in doc.modelspace():
        kind = e.dxftype()
        if kind == "INSERT":
            blocks[(e.dxf.layer, e.dxf.name)] += 1
        elif kind == "LINE":
            lengths[e.dxf.layer] += e.dxf.start.distance(e.dxf.end)
        elif kind == "LWPOLYLINE":
            lengths[e.dxf.layer] += path_length(list(e.get_points("xyb")), e.closed)
        elif kind == "POLYLINE" and e.is_2d_polyline:
            points = [(v.dxf.location.x, v.dxf.location.y, v.dxf.get("bulge", 0)) for v in e.vertices]
            lengths[e.dxf.layer] += path_length(points, e.is_closed)
    return blocks, lengths


def measured(rule: dict, blocks: Counter, lengths: dict[str, float]) -> float:
    if rule["量法"] == "数块":
        return sum(n for (layer, name), n in blocks.items()
                   if name == rule["块"] and layer == rule.get("图层", layer))
    return round(lengths.get(rule["图层"], 0.0), 2)


def label(rule: dict) -> str:
    where = f"（图层 {rule['图层']}）" if "图层" in rule else ""
    return f"{rule['量法']} {rule.get('块', '')}".strip() + where


def evaluate(dwg: Path, rules: list[dict]) -> tuple[list[dict], dict]:
    doc = load(dwg)
    blocks, lengths = measure(doc)
    tables, frames = embedded_tables(doc)
    items = [it for rows in tables for it in table_items(rows)]
    groups: dict[int, list[dict]] = defaultdict(list)
    for it in items:
        idx = next((i for i, r in enumerate(rules) if it["名称"] in r["名称"]), None)
        if idx is None:
            it["结论"] = "无规则"
        else:
            groups[idx].append(it)
    wrong = []
    for idx, members in groups.items():
        rule = rules[idx]
        want = sum(it["数量"] for it in members)
        got = measured(rule, blocks, lengths)
        ok = abs(got - want) <= (LENGTH_TOL if rule["量法"] == "线长" else 0)
        if not ok:
            wrong.append({"名称": "、".join(it["名称"] for it in members), "核对项": label(rule),
                          "设计": want, "图上": got})
        for it in members:
            it.update({"结论": "相符" if ok else "不符", "图上数量": got, "核对项": label(rule),
                       "核对项设计合计": want, "依据": rule["依据"]})
    covered = sum(len(m) for m in groups.values())
    return items, {"内嵌对象": frames, "工程量表": len(tables), "表项": len(items), "有规则表项": covered,
                   "无规则表项": len(items) - covered, "核对项": len(groups),
                   "相符": len(groups) - len(wrong), "不符": wrong}


def shown(v) -> str:
    if isinstance(v, float):
        return str(int(v)) if v == int(v) else str(round(v, 2))
    return "" if v is None else str(v)


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    # 有的原图句柄重复，ezdxf 每读一次刷一屏 WARNING，不影响评测
    logging.getLogger("ezdxf").setLevel(logging.ERROR)
    ap = argparse.ArgumentParser(description="金标准评测：内嵌工程量表对图上量出来的数量")
    ap.add_argument("raw", type=Path, help="原始图纸目录")
    ap.add_argument("out", type=Path, help="产物目录")
    ap.add_argument("--gold", type=Path, required=True, help="金标准文件，格式见 gold/示例.json")
    a = ap.parse_args()
    gold = json.loads(a.gold.read_text(encoding="utf-8"))
    base = a.raw.resolve() / gold.get("目录", "")
    out = a.out.resolve()
    out.mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    detail, per_drawing, failed = [], [], 0
    for n, rel in enumerate(gold["图纸"], 1):
        t1 = time.time()
        try:
            items, record = evaluate(base / rel, gold["规则"])
        except Exception as ex:
            failed += 1
            traceback.print_exc()
            per_drawing.append({"图纸": rel, "错误": f"{type(ex).__name__}: {str(ex)[:300]}"})
            print(f"[{n}/{len(gold['图纸'])}] 失败 {rel} -> {ex}", flush=True)
            continue
        detail += [{"图纸": rel, **it} for it in items]
        per_drawing.append({"图纸": rel, **record, "用时s": round(time.time() - t1, 1)})
        print(f"[{n}/{len(gold['图纸'])}] {time.time() - t1:.1f}s 表 {record['工程量表']} 份 {record['表项']} 项 | "
              f"核对 {record['核对项']} 项，相符 {record['相符']} | 无规则 {record['无规则表项']} 项  {rel}", flush=True)
        for w in record["不符"]:
            print(f"      不符：{w['名称']}  设计 {shown(w['设计'])}，图上 {shown(w['图上'])}（{w['核对项']}）", flush=True)

    done = [r for r in per_drawing if "错误" not in r]
    total = {k: sum(r[k] for r in done) for k in ("表项", "有规则表项", "无规则表项", "核对项", "相符")}
    summary = {"金标准": a.gold.name, "时间": time.strftime("%Y-%m-%d %H:%M:%S"), "图纸数": len(gold["图纸"]),
               "失败": failed, **total, "不符": total["核对项"] - total["相符"],
               "用时s": round(time.time() - t0, 1), "各图": per_drawing, "规则": gold["规则"]}
    (out / "评测汇总.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    with (out / "评测明细.csv").open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(COLUMNS)
        for it in detail:
            w.writerow([shown(it.get(c)) for c in COLUMNS])

    print(flush=True)
    print("=" * 50, flush=True)
    print(f"图纸 {len(done)} 张 | 表项 {total['表项']} | 核对项 {total['核对项']}：相符 {total['相符']}、"
          f"不符 {total['核对项'] - total['相符']} | 无规则表项 {total['无规则表项']} | 失败 {failed} 张 | "
          f"用时 {time.time() - t0:.1f}s", flush=True)
    print(f"输出目录: {out}", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
