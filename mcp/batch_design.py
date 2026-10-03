"""批量导出设计管网：管线、井和管件、它们的标注，连同三项自检。

    python batch_design.py <图纸目录> <图纸文字产物目录> <产物目录>

设计图里的管线、井、标注是广联达鸿业三维管线的自定义对象（CIVIL_*），无头环境里只是
代理对象，内容从代理图形里解（acad.proxy_objects）。只处理 batch_text.py 记了「代理对象」且带图形的图，
所以要先跑它。

关联规则是在管网项目的一张「管网改造平面纵断面」图上试出来的：
- 管线画到井圈为止，端点落在节点范围内（井取最外圈半径）就是它的起点、终点
- 线侧标注「管径-长度m[-坡度‰]」配给附近节点间长与标注长度相差不超过 0.6 的管线（标注长度多是整米），
  远近按字高的倍数算
- 井标注的引线端点落在井心上；文字依次是井号、设计地面标高、设计管内底标高、X-北坐标、Y-东坐标、∅井径、图集
  （两个标高的含义对过纵断面表：第二个等于「设计管内底标高」，两者之差等于「管内底埋深」）

三项自检写进每条记录：标注长度对节点间长、井标注坐标对井心、标注坡度对两端管内底标高之差。
每张图一个 JSON，全项目汇总成 设计管线.csv 和 设计节点.csv。进度实时写盘，重跑跳过已完成的。
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import re
import statistics
import sys
import time
import traceback
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import acad

PIPE = "CIVIL_PIPELINE_PIPE"
NODES = {"CIVIL_PIPE_NODE_WELL": "井", "CIVIL_PIPE_NODE_FITTING": "管件"}
PIPE_LABEL = "CIVIL_PIPE_SIDE_LABEL1"
WELL_LABEL = "CIVIL_PIPEWELL_LABEL"
# 这些类是纵断面、表格、图名比例之类，不参与平面上的管网关联；文字已经在 batch_text 的产物里
HANDLED = {PIPE, PIPE_LABEL, WELL_LABEL, *NODES}

# 线侧标注离管线多远，按字高的倍数算（图纸比例不同，字高和偏移一起变）。管网项目 5 张图实测：
# 配对正确的在 0.3~2.6 倍；有一条标的是 0.69 长的短管，引到了 4.3 倍远。
# 长度对得上的放宽到 FIT_REACH，对不上的只认 NEAR_REACH 以内最近的那根
FIT_REACH = 6.0
NEAR_REACH = 3.0
LENGTH_TOL = 0.6
SNAP = 0.05
SIDE = re.compile(r"^(?P<spec>[A-Za-zΦφ∅Ø]*\d+(?:[xX×*]\d+)?)-(?P<len>\d+(?:\.\d+)?)m(?:-(?P<slope>\d+(?:\.\d+)?)‰)?$")
NUMBER = re.compile(r"\d+(?:\.\d+)?")
TAGGED = re.compile(r"([A-Za-z东南西北]+)[:：](\d+(?:\.\d+)?)")
# 坐标要带小数：雨水井号「Y4」「Y-4」长得跟「Y-418905.265」一样，只差这一点
COORD = re.compile(r"([XY])[-=:：]?(\d+\.\d+)")
DIAMETER_MARKS = "∅ØΦφ"


def length(pts) -> float:
    return sum(math.dist(a, b) for a, b in zip(pts, pts[1:]))


def to_polyline(p, pts) -> float:
    best = math.inf
    for a, b in zip(pts, pts[1:]):
        dx, dy = b[0] - a[0], b[1] - a[1]
        t = 0 if dx == dy == 0 else max(0, min(1, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / (dx * dx + dy * dy)))
        best = min(best, math.hypot(p[0] - a[0] - t * dx, p[1] - a[1] - t * dy))
    return best


def node_of(o: dict) -> dict | None:
    if o["圆"]:
        center, radius = o["圆"][0][:2], max(c[2] for c in o["圆"])
    else:
        pts = [p for line in o["线"] for p in line]
        if not pts:
            return None
        # 雨水口解出来的多段线末尾混着坐标 (0,0) 的假点，包围盒会被拉到一百多万；用中位数定心、剔掉离群点
        center = [statistics.median(p[0] for p in pts), statistics.median(p[1] for p in pts)]
        spread = sorted(math.dist(center, p) for p in pts)
        typical = statistics.median(spread)
        radius = max(d for d in spread if d <= 5 * typical) if typical else 0.0
    return {"句柄": o["句柄"], "类别": NODES[o["类"]], "图层": o["图层"], "心": center, "半径": radius}


def parse_well_label(texts: list[str]) -> dict:
    ts = [t.strip() for t in texts]
    i_x = next((i for i, t in enumerate(ts) if i and COORD.fullmatch(t)), len(ts))
    head, tail = ts[:i_x], ts[i_x:]
    plain = [float(t) for t in head[1:] if NUMBER.fullmatch(t)]
    tagged = {m[1]: float(m[2]) for t in head[1:] if (m := TAGGED.fullmatch(t))}
    coords = {m[1]: float(m[2]) for t in tail if (m := COORD.fullmatch(t))}
    rest = [t for t in tail if not COORD.fullmatch(t)]
    # 直径符号和数字是两条文字，后面紧跟图集号「20S515」；拼成一串再找数字会读成 100020
    diameter = None
    for i, t in enumerate(rest):
        if t in DIAMETER_MARKS and i + 1 < len(rest) and rest[i + 1].isdigit():
            diameter, rest = int(rest[i + 1]), rest[:i] + rest[i + 2:]
            break
        if t[:1] in DIAMETER_MARKS and t[1:].isdigit():
            diameter, rest = int(t[1:]), rest[:i] + rest[i + 1:]
            break
    out = {"井号": head[0] if head else "", "标注": " ".join(ts)}
    if plain:
        out["设计地面标高"] = plain[0]
    if len(plain) > 1:
        out["设计管内底标高"] = plain[1]
    if tagged:
        out["各向管内底标高"] = tagged
    if coords:
        out["标注坐标"] = coords
    if diameter is not None:
        out["井径"] = diameter
    if rest:
        out["图集"] = "".join(rest)
    return out


def build(objects: list[dict]) -> dict:
    nodes = [n for o in objects if o["类"] in NODES and (n := node_of(o))]
    by_handle = {n["句柄"]: n for n in nodes}

    for o in objects:
        if o["类"] != WELL_LABEL:
            continue
        verts = [p for line in o["线"] for p in line]
        hit = next((n for v in verts for n in nodes if math.dist(v, n["心"]) < SNAP), None)
        if hit is None:
            continue
        hit.update(parse_well_label([t["文字"] for t in o["文字"]]))
        c = hit.get("标注坐标", {})
        if "X" in c and "Y" in c:
            # 测量坐标 X 是北、Y 是东，对的是图面的 y、x
            hit["坐标自检"] = "相符" if abs(c["X"] - hit["心"][1]) < 0.002 and abs(c["Y"] - hit["心"][0]) < 0.002 else "不符"

    pipes = []
    for o in objects:
        if o["类"] != PIPE or not o["线"]:
            continue
        pts = o["线"][0]
        ends = []
        for e in (pts[0], pts[-1]):
            d, n = min(((math.dist(e, n["心"]), n) for n in nodes), key=lambda x: x[0], default=(math.inf, None))
            ends.append(n if n and d <= n["半径"] + SNAP else None)
        geo = length(pts)
        pipes.append({"句柄": o["句柄"], "图层": o["图层"], "点": pts, "两端": ends, "几何长": geo,
                      "节点间长": geo + sum(math.dist(e, n["心"]) for e, n in zip((pts[0], pts[-1]), ends) if n),
                      "标注": []})

    loose, labels = [], []
    for o in objects:
        if o["类"] != PIPE_LABEL or not o["文字"]:
            continue
        text = "".join(t["文字"].strip() for t in o["文字"])
        m = SIDE.match(text)
        if not m:
            loose.append({"标注": text, "原因": "格式认不出"})
            continue
        first = o["文字"][0]
        near = sorted(((to_polyline(first["位置"], p["点"]) / first["字高"], p) for p in pipes), key=lambda x: x[0])
        fit = [(d, p) for d, p in near if d <= FIT_REACH and abs(p["节点间长"] - float(m["len"])) <= LENGTH_TOL]
        labels.append((fit[0][0] if fit else math.inf, o, m, near, fit))
    # 贴得最近的先认领，免得远处的标注先把别人的管线占了
    for _, o, m, near, fit in sorted(labels, key=lambda x: x[0]):
        # 同一处常有污水、给水两根管并行，先在同系统（图层名前两个字）里挑
        same = [x for x in fit if x[1]["图层"][:2] == o["图层"][:2]] or fit
        free = [x for x in same if not x[1]["标注"]] or same
        if free:
            free[0][1]["标注"].append(m)
        elif near and near[0][0] <= NEAR_REACH:
            near[0][1]["标注"].append(m)
        else:
            loose.append({"标注": m.group(0), "原因": "%g 倍字高以内没有管线" % NEAR_REACH})

    records = []
    for p in pipes:
        a, b = p["两端"]
        r = {"句柄": p["句柄"], "图层": p["图层"],
             "起点井号": (a or {}).get("井号", ""), "终点井号": (b or {}).get("井号", ""),
             "起点句柄": (a or {}).get("句柄", ""), "终点句柄": (b or {}).get("句柄", ""),
             "几何长": round(p["几何长"], 3), "节点间长": round(p["节点间长"], 3), "折点数": len(p["点"]),
             "点": p["点"]}
        if p["标注"]:
            m = p["标注"][0]
            r.update({"管径": m["spec"], "标注长度": float(m["len"]), "标注": " / ".join(x.group(0) for x in p["标注"])})
            if all(p["两端"]):
                r["长度自检"] = "相符" if abs(p["节点间长"] - float(m["len"])) <= LENGTH_TOL else "不符"
            if m["slope"]:
                r["坡度‰"] = float(m["slope"])
                za, zb = (a or {}).get("设计管内底标高"), (b or {}).get("设计管内底标高")
                if za is not None and zb is not None and p["节点间长"] > 0:
                    calc = abs(za - zb) / p["节点间长"] * 1000
                    # 标注坡度是整数千分比
                    r["算得坡度‰"] = round(calc, 2)
                    r["坡度自检"] = "相符" if abs(calc - r["坡度‰"]) <= max(0.5, 0.1 * r["坡度‰"]) else "不符"
        records.append(r)

    node_records = []
    for n in nodes:
        r = {k: v for k, v in n.items() if k not in ("心", "半径", "标注坐标")}
        r["X北"], r["Y东"] = round(n["心"][1], 3), round(n["心"][0], 3)
        node_records.append(r)
    return {"节点": node_records, "管线": records, "未配上的线侧标注": loose,
            "未参与关联的类": dict(Counter(o["类"] for o in objects if o["类"] not in HANDLED).most_common())}


def tally(result: dict) -> dict[str, int]:
    pipes, nodes = result["管线"], result["节点"]
    return {
        "管线": len(pipes),
        "两端都有节点": sum(1 for p in pipes if p["起点句柄"] and p["终点句柄"]),
        "有标注": sum(1 for p in pipes if "管径" in p),
        "长度相符": sum(1 for p in pipes if p.get("长度自检") == "相符"),
        "长度不符": sum(1 for p in pipes if p.get("长度自检") == "不符"),
        "坡度相符": sum(1 for p in pipes if p.get("坡度自检") == "相符"),
        "坡度不符": sum(1 for p in pipes if p.get("坡度自检") == "不符"),
        "节点": len(nodes),
        "有井号": sum(1 for n in nodes if n.get("井号")),
        "坐标相符": sum(1 for n in nodes if n.get("坐标自检") == "相符"),
        "坐标不符": sum(1 for n in nodes if n.get("坐标自检") == "不符"),
        "未配上的线侧标注": len(result["未配上的线侧标注"]),
    }


def write_csv(path: Path, rows: list[dict], cols: list[str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for r in rows:
            w.writerow([json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v
                        for v in (r.get(c, "") for c in cols)])


def main() -> int:
    ap = argparse.ArgumentParser(description="批量导出设计管网")
    ap.add_argument("raw", type=Path, help="原始图纸目录")
    ap.add_argument("texts", type=Path, help="batch_text.py 的产物目录")
    ap.add_argument("out", type=Path, help="产物目录")
    a = ap.parse_args()
    # 有的原图句柄重复，ezdxf 每读一次刷一屏 WARNING
    logging.getLogger("ezdxf").setLevel(logging.ERROR)
    raw, out = a.raw.resolve(), a.out.resolve()
    text_prog = a.texts / "_progress.json"
    if not text_prog.exists():
        print("找不到 %s，先跑 batch_text.py" % text_prog)
        return 1
    todo = sorted(Path(k) for k, v in json.loads(text_prog.read_text(encoding="utf-8")).items()
                  if v.get("代理对象", {}).get("带图形"))
    out.mkdir(parents=True, exist_ok=True)
    prog_file = out / "_progress.json"
    prog = json.loads(prog_file.read_text(encoding="utf-8")) if prog_file.exists() else {}
    print("带代理图形的图纸: %d" % len(todo), flush=True)

    failed = 0
    for i, dwg in enumerate(todo, 1):
        key = str(dwg)
        if prog.get(key, {}).get("done"):
            continue
        t1 = time.time()
        try:
            objects, stats = acad.proxy_objects(dwg)
            result = build(objects)
            counts = tally(result)
            rel = dwg.relative_to(raw)
            name = str(rel.with_suffix("")).replace("\\", "__").replace("/", "__") + ".json"
            (out / name).write_text(
                json.dumps({"图纸": key, "代理对象": stats, "统计": counts, **result}, ensure_ascii=False, indent=1),
                encoding="utf-8")
            prog[key] = {"done": True, "文件": name, "统计": counts, "耗时": round(time.time() - t1, 1)}
            print("[%d/%d] %.1fs %s  %s" % (i, len(todo), time.time() - t1,
                                            {k: v for k, v in counts.items() if v}, rel), flush=True)
        except Exception as exc:
            failed += 1
            prog[key] = {"done": False, "错误": "%s: %s" % (type(exc).__name__, exc)}
            print("[%d/%d] 失败 %s -> %s" % (i, len(todo), dwg.name, exc), flush=True)
            traceback.print_exc()
        prog_file.write_text(json.dumps(prog, ensure_ascii=False, indent=1), encoding="utf-8")

    pipes, nodes, total = [], [], Counter()
    for dwg in todo:
        p = prog.get(str(dwg), {})
        if not p.get("done"):
            continue
        d = json.loads((out / p["文件"]).read_text(encoding="utf-8"))
        where = {"_小区": dwg.parent.name, "_图纸": dwg.name}
        pipes += [{**where, **r} for r in d["管线"]]
        nodes += [{**where, **r} for r in d["节点"]]
        total.update(d["统计"])
    write_csv(out / "设计管线.csv", pipes,
              ["_小区", "_图纸", "句柄", "图层", "起点井号", "终点井号", "管径", "标注长度", "坡度‰", "算得坡度‰",
               "几何长", "节点间长", "长度自检", "坡度自检", "标注", "起点句柄", "终点句柄", "折点数", "点"])
    write_csv(out / "设计节点.csv", nodes,
              ["_小区", "_图纸", "句柄", "类别", "图层", "井号", "X北", "Y东", "设计地面标高", "设计管内底标高",
               "各向管内底标高", "井径", "图集", "坐标自检", "标注"])
    print(flush=True)
    print("=" * 50, flush=True)
    print("失败 %d | 合计 %s" % (failed, dict(total)), flush=True)
    print("输出目录: %s" % out, flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
