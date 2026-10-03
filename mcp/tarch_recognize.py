"""天正图识别：把 tarch_t3.py 的记录整理成能直接用的识别结果。

    python tarch_recognize.py <RAW目录> <产物目录> [--only 子串,子串]

每张图出 <名>_识别.json，放在记录旁边：

- 对象：每个天正对象一条 —— 类、图层、所在、范围、图面上显示的字、原文、语义属性、尺寸值、块参照、墙的基线、几何概况。
  完整几何（每条线、每个圆的坐标）在 <名>_tch.json 里，按句柄查
- 汇总：门窗表、墙、柱、房间、管线、图名、标高、轴号、尺寸值，都是从对象里按类归出来的

「文字」是图面上实际画出来的字（按行拼好）；「原文」是天正属性里录入的那串，带天正控制码
（^U上标^U、^L下标^L、^C加圈^C）。两者对不对得上由 tarch_verify.py 核对。
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import acad
from tarch_verify import (CENSUS, base_name, char_mode_height, expected_text, is_wall_baseline, load_record, matches,
                          read_summary, record_path, shown_measurement, tarch_drawings, text_lines)

CLASS_NAMES = {
    "TCH_TEXT": "单行文字", "TCH_MTEXT": "多行文字", "TCH_DIMENSION": "尺寸标注", "TCH_DIMENSION2": "尺寸标注",
    "TCH_RADIUSDIM": "半径标注", "TCH_COORD": "坐标标注", "TCH_ELEVATION": "标高标注", "TCH_MULTILEADER": "引出标注",
    "TCH_ARROW": "箭头引注", "TCH_COMPOSING": "做法标注", "TCH_DRAWINGNAME": "图名标注", "TCH_DRAWINGINDEX": "索引图名",
    "TCH_INDEXPOINTER": "索引符号", "TCH_SYMB_SECTION": "剖切符号", "TCH_CUT": "切割线", "TCH_RUPTURE": "折断线",
    "TCH_SYMMETRY": "对称轴", "TCH_NORTHTHUMB": "指北针", "TCH_MODI": "修改标记", "TCH_AXIS_LABEL": "轴号",
    "TCH_WALL": "墙", "TCH_COLUMN": "柱", "TCH_OPENING": "门窗", "TCH_SPACE": "房间", "TCH_RECTSTAIR": "双跑楼梯",
    "TCH_LINESTAIR": "直线梯段", "TCH_RAIL": "栏杆", "TCH_BLOCK_INSERT": "图块", "TCH_PATH_ARRAY": "路径阵列",
    "TCH_PIPE": "水管", "TCH_PIPEFITTING": "水管附件", "TCH_PIPEVALVE": "阀门", "TCH_EQUIPMENT": "设备",
    "TCH_VPIPEDIM": "立管标注", "TCH_WELL": "井"}
# 每个类都有、跟识别无关的属性
NOISE = {"EntityName", "EntityType", "Handle", "Layer", "LayoutRotation", "Linetype", "LinetypeScale", "Lineweight",
         "ObjectControl", "ObjectName", "Visible", "color"}
# 轴号一个对象画一排圈，每个圈里的字各是各的，不能按行拼起来
NO_MERGE = {"TCH_AXIS_LABEL"}
GEOMETRY = {"LINE": "线", "LWPOLYLINE": "多段线", "POLYLINE": "多段线", "CIRCLE": "圆", "ARC": "弧", "HATCH": "填充",
            "SOLID": "实心块", "ELLIPSE": "椭圆", "SPLINE": "样条"}
RECOGNIZED = "_识别.json"


def recognized_path(out: Path, rel: str) -> Path:
    return (out / rel).with_name(Path(rel).stem + RECOGNIZED)


def where(o: dict) -> str:
    if o.get("layout"):
        return "模型空间" if o["layout"] == "Model" else f"布局:{o['layout']}"
    return f"块:{base_name(o['owner'])}"


def text_fragments(o: dict) -> list[tuple]:
    """(u, v, 字高, 文字, 序号, 最宽能到多少)。最宽按每个字一个字高×宽度因子估，只用来判断两段字挨不挨着。"""
    out = []
    for part in o.get("parts", []):
        items = []
        if part["type"] in ("TEXT", "MTEXT") and part.get("text"):
            items.append((part["text"], part["pos"], part.get("h") or 0.0, part.get("rot") or 0.0, part.get("wf") or 1.0))
        elif part["type"] == "INSERT":
            items += [(a["text"], a["pos"], a.get("h") or 0.0, 0.0, 1.0) for a in part.get("attribs", [])
                      if a.get("text") and not a.get("invisible")]
        for text, pos, h, rot, wf in items:
            u = pos[0] * math.cos(rot) + pos[1] * math.sin(rot)
            v = -pos[0] * math.sin(rot) + pos[1] * math.cos(rot)
            out.append((u, v, h, text, len(out), len(text) * h * max(wf, 0.5) * 1.05))
    return out


def display_lines(o: dict) -> list[str]:
    """对象画在图面上的字，一行一条。同一行里挨着的碎片（换字体、上下标拆出来的）接成一串，隔开的各算一条。"""
    frags = text_fragments(o)
    if not frags:
        return []
    if o["dxf"] in NO_MERGE:
        return [acad.clean_text(f[3]) for f in frags]
    lines = []
    for ln in sorted(text_lines(frags, char_mode_height(frags)), key=lambda ln: -ln["y"]):
        run, reach = "", None
        for f in sorted(ln["items"]):
            if reach is not None and f[0] > reach + 0.6 * f[2]:
                lines.append(run)
                run = ""
            run += f[3]
            reach = f[0] + f[5]
        if run:
            lines.append(run)
    return [acad.clean_text(s) for s in lines if s.strip()]


def describe(o: dict) -> dict:
    item = {"句柄": o["h"], "类": CLASS_NAMES.get(o["dxf"], o["dxf"]), "天正类": o["cls"], "图层": o.get("layer", ""),
            "所在": where(o)}
    if "ext" in o:
        item["范围"] = o["ext"]
    lines = display_lines(o)
    if lines:
        item["文字"] = lines
    original = expected_text(o)
    if original.strip():
        item["原文"] = original
    props = {k: v for k, v in (o.get("props") or {}).items() if k not in NOISE and v not in ("", None)}
    if props:
        item["属性"] = props
    dims = [{"显示": shown_measurement(p), "测量": p.get("meas"), "起点": p.get("x1") or p.get("c"),
             "终点": p.get("x2") or p.get("chord")} for p in o.get("parts", []) if p["type"] == "DIMENSION"]
    if dims:
        item["尺寸"] = dims
    blocks = [{"名": base_name(p["name"]), "位置": p["pos"], "转角": p.get("rot"), "比例": p.get("scale")}
              for p in o.get("parts", []) if p["type"] == "INSERT"]
    if blocks:
        item["图块"] = blocks
    base = next((p for p in o.get("parts", []) if is_wall_baseline(o, p)), None)
    if base:
        # 墙的基线是定位线，图面上不显示，但它就是这段墙从哪到哪
        item["基线"] = [base["p1"], base["p2"]]
        item["长"] = round(math.dist(base["p1"], base["p2"]), 3)
    shapes = Counter(GEOMETRY[p["type"]] for p in o.get("parts", []) if p["type"] in GEOMETRY and p is not base)
    if shapes:
        item["几何"] = dict(shapes)
    broken = sorted({p.get("dxf") or o["dxf"] for p in o.get("parts", []) if p["type"] == "!"} | ({o["dxf"]} if "err" in o else set()))
    if broken:
        # 嵌在里面的天正对象在内存里分解不了（楼梯里的方向箭头），它画的线这里没有，天正3 里有
        item["没分解出来的部分"] = [CLASS_NAMES.get(d, d) for d in broken]
    return item


def number(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def tidy(v):
    """天正给的尺寸带浮点尾数（3000.0000088），归表之前抹到千分位，不然同一种门窗会分成两行。对象里留原值。"""
    if isinstance(v, float):
        r = round(v, 3)
        return int(r) if r == int(r) else r
    return v


def tables(items: list[dict]) -> dict:
    """从对象里按类归出来的几张表。属性名是天正 COM 的原名，值是天正给的原值。"""
    by = defaultdict(list)
    for it in items:
        by[it["类"]].append(it)
    out: dict = {}

    openings = defaultdict(Counter)
    info = {}
    for it in by["门窗"]:
        p = it.get("属性", {})
        key = p.get("Label") or "(无编号)"
        openings[key][(tidy(p.get("Width")), tidy(p.get("Height")))] += 1
        info[key] = p
    if openings:
        out["门窗表"] = [{"编号": k, "类型": f"{info[k].get('GetKind', '')}/{info[k].get('GetSubKind', '')}".strip("/"),
                        "宽": w, "高": h, "窗台高": tidy(info[k].get("WinSill")), "数量": n}
                       for k in sorted(openings) for (w, h), n in sorted(openings[k].items(), key=str)]

    walls = defaultdict(lambda: [0, 0.0, 0])
    for it in by["墙"]:
        p = it.get("属性", {})
        slot = walls[(tidy(p.get("TotalWidth")), tidy(p.get("Height")), p.get("Usage") or "")]
        slot[0] += 1
        slot[1] += it.get("长", 0.0)
        slot[2] += "长" not in it
    if walls:
        out["墙"] = [{"厚": w, "高": h, "用途": u, "段数": n, "基线总长": round(length, 3), **({"没有基线的段数": missing} if missing else {})}
                    for (w, h, u), (n, length, missing) in sorted(walls.items(), key=str)]

    columns = Counter((it.get("属性", {}).get("SectionShapeText") or it.get("属性", {}).get("Style") or "",
                       tidy(it.get("属性", {}).get("ColumnWidthB")), tidy(it.get("属性", {}).get("ColumnHeightH")),
                       tidy(it.get("属性", {}).get("Height"))) for it in by["柱"])
    if columns:
        out["柱"] = [{"截面": s, "宽B": b, "高H": h, "柱高": z, "数量": n} for (s, b, h, z), n in sorted(columns.items(), key=str)]

    if by["房间"]:
        out["房间"] = [{"名称": it.get("属性", {}).get("Name", ""), "编号": it.get("属性", {}).get("Code", ""),
                      "使用面积": it.get("属性", {}).get("UseArea"), "周长": tidy(it.get("属性", {}).get("Perimeter")),
                      "句柄": it["句柄"]} for it in by["房间"]]

    pipes = defaultdict(lambda: [0, 0.0])
    for it in by["水管"]:
        p = it.get("属性", {})
        slot = pipes[(p.get("SysType") or "", tidy(p.get("DNDiameter")))]
        slot[0] += 1
        slot[1] += number(p.get("Length")) or 0.0
    if pipes:
        out["水管"] = [{"系统": s, "管径DN": d, "段数": n, "总长": round(length, 3)}
                     for (s, d), (n, length) in sorted(pipes.items(), key=str)]

    for kind, key in (("水管附件", "FittingType"), ("阀门", None), ("设备", None), ("图块", None)):
        count = Counter()
        for it in by[kind]:
            name = it.get("属性", {}).get(key) if key else None
            count[name or "、".join(sorted({b["名"] for b in it.get("图块", [])})) or "(无图形)"] += 1
        if count:
            out[kind] = [{"名称": k, "数量": n} for k, n in count.most_common()]

    names = [{"图名": it.get("属性", {}).get("NameText", ""), "比例": it.get("属性", {}).get("ScaleText", ""),
              "所在": it["所在"]} for it in by["图名标注"] + by["索引图名"] if it.get("属性", {}).get("NameText")]
    if names:
        out["图名"] = names
    if by["标高标注"]:
        out["标高"] = sorted({ln for it in by["标高标注"] for ln in it.get("文字", [])})
    if by["轴号"]:
        out["轴号"] = sorted({ln for it in by["轴号"] for ln in it.get("文字", [])})
    dims = Counter(d["显示"] for it in by["尺寸标注"] + by["半径标注"] for d in it.get("尺寸", []))
    if dims:
        out["尺寸值"] = {"标注段数": sum(dims.values()), "按显示值计数": dict(dims.most_common())}
    return out


def recognize(objs: list[dict], rel: str = "", differs: dict | None = None) -> dict:
    """differs 是三方核对给的「句柄 → 有几个图元和天正3 不一样」（tarch_verify 结果里的「几何差异对象」）。"""
    items = [describe(o) for o in objs]
    for it in items:
        if (differs or {}).get(it["句柄"]):
            # 分解出来的线和图面上显示的不全一样（楼梯踏步线伸进扶手、墙端多一条封口线），显示的样子以天正3 为准
            it["与天正3不同的几何图元数"] = differs[it["句柄"]]
    texts = [ln for it in items for ln in it.get("文字", [])]
    return {"图纸": rel, "天正对象": len(items), "按类": dict(Counter(it["类"] for it in items).most_common()),
            "文字条数": len(texts), "汇总": tables(items), "对象": items}


def recognize_file(out: Path, rel: str, verified: dict | None = None) -> dict:
    """verified 是这张图的三方核对结果；给了就把几何对不上的对象标出来。"""
    result = recognize(load_record(record_path(out, rel)), rel, (verified or {}).get("几何差异对象"))
    recognized_path(out, rel).write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    return result


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="天正图识别：记录 → 识别结果")
    ap.add_argument("raw", type=Path, help="原始图纸目录")
    ap.add_argument("out", type=Path, help="tarch_t3.py 的产物目录")
    ap.add_argument("--only", default="", help="只处理路径里含这些子串的图，逗号分隔")
    a = ap.parse_args()
    census = a.out / CENSUS
    if not census.exists():
        print(f"找不到 {census}，先跑 tarch_t3.py")
        return 1
    todo = [rel for rel in tarch_drawings(json.loads(census.read_text(encoding="utf-8"))) if matches(rel, a.only)]
    t0 = time.time()
    total, failed = Counter(), 0
    # 核对过的图，把几何和天正3 对不上的对象标出来；没核对过的照常识别，只是不带这个标记
    verified = read_summary(a.out)
    for n, rel in enumerate(todo, 1):
        if not record_path(a.out, rel).exists():
            failed += 1
            print(f"[{n}/{len(todo)}] 没有记录，先跑 tarch_t3.py  {rel}", flush=True)
            continue
        r = recognize_file(a.out, rel, verified.get(rel))
        total.update(r["按类"])
        print(f"[{n}/{len(todo)}] 对象 {r['天正对象']} 文字 {r['文字条数']} 条 汇总 {list(r['汇总'])}  {rel}", flush=True)
    print(f"识别结束：{len(todo)} 张，缺记录 {failed} 张，{time.time() - t0:.1f}s；按类 {dict(total.most_common())}", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
