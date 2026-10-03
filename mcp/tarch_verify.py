"""原图、天正记录、天正3 三方逐张核对。

记录（<名>_tch.json，tarch_t3.py 写的）里每个天正对象带着它分解出来的图元和属性；天正3 是天正自己导出的整图。
两样东西出自天正的两条路（逐个对象内存分解 / TSAVEAS 整图导出），互相对得上才算数：

- 代理清零：天正3 里没有代理对象
- 对象齐全：原图里每个天正代理对象都有记录，句柄一一对应
- 分解对得上天正3：记录里每个图元都能在天正3 的同一个块里找到，类型、几何、文字相同
- 文字增量相等：天正3 比原图多出来的文字，正好是记录里的文字（按内容计数）
- 原文拼得回：有原文属性的对象（文字、引注、标高、坐标、门窗编号……），它自己分解出的碎片能拼回原文
- 尺寸值：按记录里的测量值和标注样式算出的显示值，等于天正3 里那个标注实际画出来的字
- 原图文字不丢、无乱码，OLE、视口数不变

    python tarch_verify.py <RAW目录> <产物目录> [--only 子串,子串] [--redo]

逐张追加到 <产物目录>/_核对汇总.jsonl，同一张图以最后一行为准；核对过的跳过，出错的下次重跑。
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import re
import sys
import time
import traceback
import uuid
from collections import Counter, defaultdict
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import ezdxf
from ezdxf import disassemble

sys.path.insert(0, str(Path(__file__).resolve().parent))
import acad

CENSUS = "_天正普查.json"
SUMMARY = "_核对汇总.jsonl"
RECORD_VERSION = 2
TEXT_KEYS = ("Text", "UpText", "DownText", "IndexLabel", "NameText", "Contents", "TextContent")
PLACEHOLDER = re.compile(r"^TDb\w+ \(TCH_\w+\)$")
MARKUP = re.compile(r"\^[UCL]")
# 天正符号码导出后画成几何而不是字：^1 变成 圆+竖线 的直径符号
SYMBOL = re.compile(r"\^\d")
NUMBER = re.compile(r"[+-]?(\d+\.?\d*|\.\d+)")
# GBK 双字节被当单字节解：前字节变成 U+0080–U+00FF 里的字符，后字节变成“?”（见 acad.FONTS）
GARBLED = re.compile(r"[\u0080-ÿ]\?")
MTEXT_CODE = re.compile(r"\\[AaCcFfHhQqTtWwLlOoKk][^;\\]*;|\\[LlOoKkPpNn~]|[{}]")
# 坐标对不对得上的容差：图形单位的千分之一，外加大坐标的浮点误差（有的图画在 3×10⁹ 的坐标上）
ABS_TOL, REL_TOL = 1e-3, 1e-9
CELL = 1.0
# 两个定义点谁先谁后都算同一个图元。不能靠排序统一顺序：竖线两端的 x 只差浮点误差，排出来的先后是随机的
UNORDERED = {"LINE", "DIMENSION"}


def t3_path(out: Path, rel: str) -> Path:
    return (out / rel).with_name(Path(rel).stem + "_t3.dwg")


def record_path(out: Path, rel: str) -> Path:
    return (out / rel).with_name(Path(rel).stem + "_tch.json")


def matches(rel: str, only: str) -> bool:
    return not only or any(k in rel for k in only.split(","))


def tarch_drawings(census: dict) -> list[str]:
    return [rel for rel, r in census.items() if any(k.startswith("TCH_") for k in r.get("代理", {}))]


def load_record(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("版本") != RECORD_VERSION:
        raise ValueError(f"{path.name} 是旧格式的记录，用 tarch_t3.py --rerecord 重写")
    return data["对象"]


def norm(s: str) -> str:
    # 天正成对标记：^U上标^U（866m^U2^U = 866m²）、^C加圈^C、^L下标^L；导出后都是独立的小字
    s = SYMBOL.sub("", MARKUP.sub("", s))
    s = re.sub("%%[pP]", "±", re.sub("%%[cC]", "Ø", re.sub("%%[dD]", "°", s)))
    return "".join(s.split())


def decimals(precision) -> int:
    p = str(precision or "")
    return len(p.split(".")[-1]) if "." in p else 0


def elevation_text(p) -> str:
    """标高的 Text 是录入值，图面按 Precision 补位、0 显示成 ±0.000；& 后是注字，$ 分隔多层标高。"""
    d = decimals(p.get("Precision", "0.000"))
    items = []
    for item in str(p.get("Text", "")).split("$"):
        val, *notes = item.split("&")
        if NUMBER.fullmatch(val.strip()):
            x = float(val)
            val = "±" + f"{0:.{d}f}" if x == 0 else f"{x:.{d}f}"
        items.append(val + "".join(notes))
    return "".join(items)


def expected_texts(o: dict) -> list[str]:
    """对象的原文：属性里记着、图面上应该画出来的字，一个属性一条。没有这种属性的类返回空表。"""
    p = o.get("props") or {}
    cls = o.get("cls")
    if cls == "TDbSymbCoord" and isinstance(p.get("XValue"), (int, float)):
        d = decimals(p.get("Precision", "0.000"))
        return [f"X={p['XValue']:.{d}f}Y={p['YValue']:.{d}f}"]
    if cls == "TDbSymbElevation" and p.get("Text"):
        return [elevation_text(p)]
    if cls in ("TDbOpening", "TDbGroupOpening"):
        # HideLabel=是 时门窗编号不画出来，不能算进应有的原文
        return [p["Label"]] if p.get("HideLabel") == "否" and p.get("Label") else []
    if cls == "TDbSymbModi":
        # 旧改项目 10 个修改标记 EditText 都是「否」，TextContent 都是 "1"，分解结果和天正3 里都没有这个字：没画
        return [str(p["TextContent"])] if p.get("TextContent") and p.get("EditText") != "否" else []
    if cls == "TDbRadiusDim":
        # Text 是量出来的半径，DesText 是改写；有改写图面上画的是改写
        return [str(p.get("DesText") or p.get("Text") or "")]
    return [str(p[k]) for k in TEXT_KEYS if p.get(k) and str(p[k]).strip()]


def expected_text(o: dict) -> str:
    return " ".join(expected_texts(o))


def base_name(name: str) -> str:
    # 块里含天正对象时，天正3导出会把块参照改名为 原名_~F
    return name[:-3] if name.endswith("_~F") else name


def fragments(o: dict) -> list[tuple]:
    """对象分解出的文字碎片 (u, v, 字高, 文字, 序号)：u、v 是沿文字方向和垂直方向的坐标，转角不同的图也能排阅读顺序。"""
    out = []
    for i, part in enumerate(o.get("parts", [])):
        items = []
        if part["type"] in ("TEXT", "MTEXT") and part.get("text"):
            items.append((part["text"], part["pos"], part.get("h") or 0.0, part.get("rot") or 0.0))
        elif part["type"] == "INSERT":
            # 天正把一部分门窗编号导成门窗块的属性而不是独立文字，属性也得算碎片
            items += [(a["text"], a["pos"], a.get("h") or 0.0, 0.0) for a in part.get("attribs", [])
                      if a.get("text") and not a.get("invisible")]
        for text, pos, h, rot in items:
            u = pos[0] * math.cos(rot) + pos[1] * math.sin(rot)
            v = -pos[0] * math.sin(rot) + pos[1] * math.cos(rot)
            out.append((u, v, h, text, f"{i}:{len(out)}"))
    return out


def char_mode_height(frags) -> float:
    """覆盖字符最多的字高。混进来的一个大号单字（如旁边的“2组”的“2”）不该把正文降级成上标。"""
    weight = Counter()
    for f in frags:
        weight[round(f[2], 6)] += len(norm(f[3])) or 1
    return max(weight.items(), key=lambda kv: (kv[1], kv[0]))[0]


def text_lines(frags, hmax=None) -> list[dict]:
    """把碎片归成行：字高够大的按基线聚成行，小字（上下标）并到最近的行里。"""
    if not frags:
        return []
    hmax = hmax or max(f[2] for f in frags) or 1.0
    lines = []
    for f in sorted([f for f in frags if f[2] >= 0.8 * hmax], key=lambda f: -f[1]):
        for ln in lines:
            if abs(f[1] - ln["y"]) <= 0.35 * hmax:
                ln["items"].append(f)
                break
        else:
            lines.append({"y": f[1], "items": [f]})
    for f in [f for f in frags if f[2] < 0.8 * hmax]:
        if not lines:
            lines.append({"y": f[1], "items": []})
        min(lines, key=lambda ln: abs(f[1] - ln["y"]))["items"].append(f)
    return lines


def reading_order(frags, hmax=None, upward=False):
    # 多层标高（$ 分隔）第一层贴着标高线、往上叠，阅读顺序是自下而上
    lines = text_lines(frags, hmax)
    return [f for ln in sorted(lines, key=lambda ln: ln["y"] if upward else -ln["y"]) for f in sorted(ln["items"])]


def align(ordered, target: str):
    """按阅读顺序挑一个子序列，拼起来恰好等于原文；对象自己的别的字（图号、比例）跳过。
    返回 (是否完整覆盖, 拼出的串)。不能贪心：“AXIS”会抢先吃掉“AXIS_TEXT”的开头，所以按 已对齐位置→路径 做子序列 DP。"""
    states = {0: []}
    for f in ordered:
        t = norm(f[3])
        if not t:
            continue
        new = {ptr + len(t): used + [f] for ptr, used in states.items()
               if target.startswith(t, ptr) and ptr + len(t) not in states}
        for k, v in new.items():
            states.setdefault(k, v)
    used = states.get(len(target)) or states[max(states)]
    return len(target) in states, "".join(f[3] for f in used)


def restore_one(frags, target: str) -> tuple[bool, str]:
    if not frags:
        return False, ""
    hm = char_mode_height(frags)
    best = (False, "")
    for order in (reading_order(frags), reading_order(frags, hm), reading_order(frags, hm, upward=True)):
        hit = align(order, target)
        if hit[0]:
            return hit
        if len(hit[1]) > len(best[1]):
            best = hit
    return best


def restore(o: dict) -> tuple[bool, str]:
    """对象自己的碎片能不能拼回它的每一条原文。返回 (全部拼回, 拼出的串)。
    一个属性一条分开拼：索引符号的上文字、下文字、圈里的编号在图面上的先后跟属性的先后不是一回事。"""
    frags = fragments(o)
    hits = [restore_one(frags, t) for t in map(norm, expected_texts(o)) if t]
    return bool(hits) and all(h[0] for h in hits), " ".join(h[1] for h in hits)


def shown_measurement(part: dict) -> str:
    """标注图面上显示的字：有改写用改写（<> 代表测量值），否则按样式的比例因子、精度、消零排测量值。"""
    meas = part.get("meas")
    text = ""
    if meas is not None:
        v = Decimal(repr(meas * (part.get("lfac") or 1.0)))
        rnd = part.get("rnd") or 0
        if rnd > 0:
            v = (v / Decimal(repr(rnd))).quantize(Decimal(1), rounding=ROUND_HALF_UP) * Decimal(repr(rnd))
        text = str(v.quantize(Decimal(1).scaleb(-int(part.get("dec") or 0)), rounding=ROUND_HALF_UP))
        zin = int(part.get("zin") or 0)
        if zin & 8 and "." in text:
            text = text.rstrip("0").rstrip(".")
        if zin & 4 and text.startswith("0."):
            text = text[1:]
        post = part.get("post") or ""
        text = post.replace("<>", text) if "<>" in post else text + post
        if part.get("kind") == "RadialDimension":
            text = "R" + text
        elif part.get("kind") == "DiametricDimension":
            text = "Ø" + text
    override = part.get("override") or ""
    return override.replace("<>", text) if override else text


def plain(s: str) -> str:
    """去掉 MTEXT 格式码和天正、AutoCAD 的控制码之后的字，用来比两边显示的是不是同一串。"""
    return norm(MTEXT_CODE.sub("", s))


def close(a: float, b: float) -> bool:
    return abs(a - b) <= ABS_TOL + REL_TOL * max(abs(a), abs(b))


def same_points(a, b) -> bool:
    return len(a) == len(b) and all(close(p[0], q[0]) and close(p[1], q[1]) for p, q in zip(a, b))


def xy(p) -> tuple[float, float]:
    return float(p[0]), float(p[1])


def entity_shape(e) -> tuple | None:
    """ezdxf 图元 → (类型, 文字, 点列, 数值)。只比得出位置的类型有返回值。"""
    kind = e.dxftype()
    d = e.dxf
    if kind == "LINE":
        return kind, "", [xy(d.start), xy(d.end)], ()
    if kind == "CIRCLE":
        return kind, "", [xy(d.center)], (d.radius,)
    if kind == "ARC":
        return kind, "", [xy(d.center)], (d.radius,)
    if kind in ("TEXT", "ATTRIB"):
        return "TEXT", d.text, [xy(d.insert)], ()
    if kind == "MTEXT":
        return kind, plain(e.text), [xy(d.insert)], ()
    if kind == "LWPOLYLINE":
        return kind, "", [xy(p) for p in e.get_points("xy")], ()
    if kind == "POLYLINE":
        return "LWPOLYLINE", "", [xy(v.dxf.location) for v in e.vertices], ()
    if kind == "INSERT":
        return kind, base_name(d.name).upper(), [xy(d.insert)], ()
    if kind == "DIMENSION":
        pts = [d.get("defpoint2"), d.get("defpoint3")] if (d.get("dimtype", 0) & 7) in (0, 1) else [d.get("defpoint"), d.get("defpoint4")]
        return kind, "", [xy(p) for p in pts if p is not None], ()
    if kind == "HATCH":
        first = None
        if len(e.paths):
            path = e.paths[0]
            if hasattr(path, "vertices") and path.vertices:
                first = xy(path.vertices[0])
            elif getattr(path, "edges", None) and hasattr(path.edges[0], "start"):
                first = xy(path.edges[0].start)
        return kind, d.get("pattern_name", ""), [first] if first else [], ()
    if kind in ("SOLID", "TRACE"):
        return "SOLID", "", sorted(xy(d.get(k)) for k in ("vtx0", "vtx1", "vtx2", "vtx3") if d.get(k) is not None), ()
    if kind == "ELLIPSE":
        return kind, "", [xy(d.center)], ()
    if kind == "SPLINE":
        return kind, "", [xy(p) for p in e.control_points], ()
    if kind == "LEADER":
        return kind, "", [xy(p) for p in e.vertices], ()
    if kind == "POINT":
        return kind, "", [xy(d.location)], ()
    return None


def part_shape(p: dict) -> tuple | None:
    """记录里的图元 → 与 entity_shape 同样的形状。"""
    kind = p["type"]
    if kind == "LINE":
        return kind, "", [xy(p["p1"]), xy(p["p2"])], ()
    if kind in ("CIRCLE", "ARC"):
        return kind, "", [xy(p["c"])], (p["r"],)
    if kind == "TEXT":
        return kind, p["text"], [xy(p["pos"])], ()
    if kind == "MTEXT":
        return kind, plain(p.get("raw") or p["text"]), [xy(p["pos"])], ()
    if kind in ("LWPOLYLINE", "POLYLINE"):
        return "LWPOLYLINE", "", [xy(q) for q in p["pts"]], ()
    if kind == "INSERT":
        return kind, base_name(p["name"]).upper(), [xy(p["pos"])], ()
    if kind == "DIMENSION":
        pts = [p.get("x1"), p.get("x2")] if "x1" in p else [p.get("c") or p.get("far"), p.get("chord")]
        return kind, "", [xy(q) for q in pts if q is not None], ()
    if kind == "HATCH":
        loops = p.get("loops") or []
        return kind, p.get("pattern", ""), [xy(loops[0][0])] if loops and loops[0] else [], ()
    if kind in ("SOLID", "TRACE"):
        return "SOLID", "", sorted(xy(q) for q in p["pts"]), ()
    if kind == "ELLIPSE":
        return kind, "", [xy(p["c"])], ()
    if kind in ("SPLINE", "LEADER"):
        return kind, "", [xy(q) for q in p["pts"]], ()
    if kind == "POINT":
        return kind, "", [xy(p["pos"])], ()
    return None


def straight_segments(pts, closed: bool, bulges=None) -> list[tuple]:
    """多段线的直线段 ((x1, y1), (x2, y2))；带凸度的弧段不算。"""
    out = []
    n = len(pts)
    for i in range(n if closed else n - 1):
        if bulges and bulges[i]:
            continue
        a, b = pts[i], pts[(i + 1) % n]
        if a != b:
            out.append((a, b))
    return out


def entity_segments(e) -> list[tuple]:
    kind = e.dxftype()
    if kind == "LINE":
        return [(xy(e.dxf.start), xy(e.dxf.end))]
    if kind == "LWPOLYLINE":
        pts = e.get_points("xyb")
        return straight_segments([xy(q) for q in pts], bool(e.closed), [q[2] for q in pts])
    if kind == "POLYLINE":
        return straight_segments([xy(v.dxf.location) for v in e.vertices], bool(e.is_closed), [v.dxf.get("bulge", 0) for v in e.vertices])
    return []


class Linework:
    """一堆直线段的网格索引，回答「这条线段是不是整段都画了」。

    天正3 和逐个分解的线不是一条对一条：相邻的墙线在天正3 里可能连成一条，或者画成多段线、放进块里。
    所以不比图元，比覆盖 —— 记录里的线段，落在同一条直线上的天正3 线段拼起来能不能盖住它。
    """
    # 一条线段最多登记到这么多格；再长的（图框边那种）单独放一堆，每次都查
    MAX_CELLS = 4000

    def __init__(self, segments: list[tuple]):
        self.segs = segments
        lengths = sorted(math.dist(a, b) for a, b in segments)
        self.g = max(lengths[len(lengths) // 2] * 2, 1e-6) if lengths else 1.0
        self.grid = defaultdict(list)
        self.long = []
        for i, (a, b) in enumerate(segments):
            cells = self.cells(a, b)
            if cells is None:
                self.long.append(i)
            else:
                for c in cells:
                    self.grid[c].append(i)

    def cells(self, a, b) -> set | None:
        n = int(math.dist(a, b) / (self.g / 2)) + 1
        if n > self.MAX_CELLS:
            return None
        return {(math.floor((a[0] + (b[0] - a[0]) * k / n) / self.g), math.floor((a[1] + (b[1] - a[1]) * k / n) / self.g))
                for k in range(n + 1)}

    def covered(self, a, b) -> bool:
        length = math.dist(a, b)
        tol = ABS_TOL * 10 + REL_TOL * max(abs(a[0]), abs(a[1]), abs(b[0]), abs(b[1]))
        if length <= tol:
            return True
        ux, uy = (b[0] - a[0]) / length, (b[1] - a[1]) / length
        cells = self.cells(a, b)
        near = set(self.long)
        if cells is None:
            near.update(range(len(self.segs)))
        else:
            for cx, cy in cells:
                for dx in (-1, 0, 1):
                    for dy in (-1, 0, 1):
                        near.update(self.grid.get((cx + dx, cy + dy), ()))
        spans = []
        for i in near:
            c, d = self.segs[i]
            # 共线：对方两个端点都贴着这条线，或者这条线的两个端点都贴着对方（对方长得多时方向更准）
            off = [abs((q[0] - a[0]) * uy - (q[1] - a[1]) * ux) for q in (c, d)]
            if max(off) > tol:
                other = math.dist(c, d)
                vx, vy = (d[0] - c[0]) / other, (d[1] - c[1]) / other
                if max(abs((q[0] - c[0]) * vy - (q[1] - c[1]) * vx) for q in (a, b)) > tol:
                    continue
            t = sorted((q[0] - a[0]) * ux + (q[1] - a[1]) * uy for q in (c, d))
            spans.append(t)
        reach = 0.0
        for t0, t1 in sorted(spans):
            if t0 > reach + tol:
                break
            reach = max(reach, t1)
            if reach >= length - tol:
                return True
        return False


class BlockIndex:
    """一个块里的图元建索引，供记录里的图元来认领；一个图元只能被认领一次。

    文字按内容找最近的（两条路排出来的基线会差零点几个字高）；别的按 (类型, 首点所在格子) 找坐标相同的；
    直线段另有覆盖查询（见 Linework）。
    """

    def __init__(self, entities):
        self.cells = defaultdict(list)
        self.texts = defaultdict(list)
        self.inserts = []
        self.segments = []
        self.linework = None
        self.inner = {}
        for e in entities:
            self.add(e)
            if e.dxftype() == "INSERT":
                self.inserts.append(e)
                # 门窗编号、轴号在天正3 里是块参照的属性，分解出来的是独立文字，位置和内容相同
                for a in e.attribs:
                    self.add(a)

    def add(self, e) -> None:
        shape = entity_shape(e)
        if shape is None:
            return
        item = {"shape": shape, "entity": e, "taken": False}
        if shape[0] in ("TEXT", "MTEXT"):
            self.texts[plain(shape[1])].append(item)
        else:
            self.cells[self.key(shape)].append(item)
            if shape[0] in UNORDERED and len(shape[2]) == 2:
                self.cells[self.key((shape[0], "", shape[2][::-1], ()))].append(item)
            self.segments += entity_segments(e)

    @staticmethod
    def key(shape, dx=0, dy=0):
        kind, _, pts, _ = shape
        if not pts:
            return kind, None
        return kind, (math.floor(pts[0][0] / CELL) + dx, math.floor(pts[0][1] / CELL) + dy)

    def claim(self, shape):
        kind, text, pts, nums = shape
        shifts = [(0, 0)] if not pts else [(dx, dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1)]
        for dx, dy in shifts:
            for item in self.cells.get(self.key(shape, dx, dy), ()):
                k2, t2, p2, n2 = item["shape"]
                if (not item["taken"] and t2 == text and len(nums) == len(n2) and all(close(a, b) for a, b in zip(nums, n2))
                        and (same_points(pts, p2) or (kind in UNORDERED and same_points(pts, p2[::-1])))):
                    item["taken"] = True
                    return item["entity"]
        return None

    def claim_text(self, text: str, pos, reach: float):
        """内容相同、离 pos 最近、且在 reach 之内的那条没被认领的文字。"""
        best, best_d = None, reach
        for item in self.texts.get(plain(text), ()):
            if not item["taken"]:
                d = math.dist(item["shape"][2][0], pos)
                if d <= best_d:
                    best, best_d = item, d
        if best is None:
            return None
        best["taken"] = True
        return best["entity"]

    def covered(self, a, b) -> bool:
        if self.linework is None:
            self.linework = Linework(self.segments)
        return self.linework.covered(a, b)

    def inside(self, ext) -> "BlockIndex":
        """插入点落在 ext 范围里的块参照，展开之后的图元。柱框、轴号圈、门窗在天正3 里是块参照，分解出来的是线和圆。"""
        margin = 0.05 * max(ext[2] - ext[0], ext[3] - ext[1]) + ABS_TOL
        hits = [e for e in self.inserts if ext[0] - margin <= e.dxf.insert[0] <= ext[2] + margin
                and ext[1] - margin <= e.dxf.insert[1] <= ext[3] + margin]
        key = tuple(id(e) for e in hits)
        if key not in self.inner:
            flat = []
            for e in hits:
                try:
                    flat += [v for v in disassemble.recursive_decompose([e]) if v.dxftype() != "ATTRIB"]
                except Exception:
                    continue
            self.inner[key] = BlockIndex(flat)
        return self.inner[key]


def match_part(blk: BlockIndex, p: dict, shape: tuple, reach: float = 0.0):
    """记录里的一个图元到 blk 里去认领。返回认领到的天正3 图元；线段靠覆盖对上的没有对应的单个图元，返回 True。
    reach 是文字位置允许差多少：分解和导出两条路排出来的基线差零点几个字高，上下标跟着正文一起偏。"""
    kind = shape[0]
    if kind in ("TEXT", "MTEXT"):
        return blk.claim_text(shape[1], shape[2][0], max(reach, 0.5 * (p.get("h") or 0.0), ABS_TOL))
    hit = blk.claim(shape)
    if hit is not None:
        return hit
    if p["type"] == "LINE":
        return blk.covered(xy(p["p1"]), xy(p["p2"])) or None
    if p["type"] in ("LWPOLYLINE", "POLYLINE") and not any(q[2] for q in p["pts"]):
        segs = straight_segments([xy(q) for q in p["pts"]], bool(p.get("closed")))
        return (bool(segs) and all(blk.covered(a, b) for a, b in segs)) or None
    return None


def exploded_dimension_text(blk: BlockIndex, p: dict):
    """标注在天正3 里被分解成线和字时（块里的老式标注），按显示值在标注附近找那条字。"""
    pts = [xy(p[k]) for k in ("x1", "x2", "line", "c", "chord", "far") if p.get(k)]
    want = shown_measurement(p)
    if not pts or not plain(want):
        return None
    cx, cy = sum(q[0] for q in pts) / len(pts), sum(q[1] for q in pts) / len(pts)
    return blk.claim_text(want, (cx, cy), 2 * max(math.dist(q, (cx, cy)) for q in pts) + ABS_TOL)


POINT_KEYS = ("p1", "p2", "c", "pos", "x1", "x2", "line", "chord", "far", "textpos")
IDENTITY = (1.0, 0.0, 0.0, 1.0)


def linear(ref) -> tuple[float, float, float, float]:
    """块参照把块内坐标带到外面去的线性部分 (a, b, c, d)：x' = a·x + c·y，y' = b·x + d·y。"""
    sx, sy = ref.dxf.get("xscale", 1.0), ref.dxf.get("yscale", 1.0)
    r = math.radians(ref.dxf.get("rotation", 0.0))
    return sx * math.cos(r), sx * math.sin(r), -sy * math.sin(r), sy * math.cos(r)


def between(raw_ref, t3_ref) -> tuple | None:
    """原图的块参照和天正3 里顶替它的块参照落在同一处时，原图块内坐标 → 天正3 块内坐标的变换：T3⁻¹·RAW。"""
    a, b, c, d = linear(t3_ref)
    det = a * d - b * c
    if abs(det) < 1e-12:
        return None
    p, q, r, s = linear(raw_ref)
    m = ((d * p - c * q) / det, (a * q - b * p) / det, (d * r - c * s) / det, (a * s - b * r) / det)
    return tuple(round(v, 9) + 0.0 for v in m)


def moved(p: dict, m: tuple) -> dict:
    """记录里的一个图元按块内坐标的变换 m 挪过去。只挪核对用得到的坐标，转角、字高不动。"""
    a, b, c, d = m
    det = a * d - b * c

    def at(q):
        return [a * q[0] + c * q[1], b * q[0] + d * q[1]]

    out = dict(p)
    for k in POINT_KEYS:
        if p.get(k):
            out[k] = at(p[k])
    if p.get("pts"):
        # 第三个数是多段线的凸度，镜像之后弧朝另一边鼓
        out["pts"] = [at(q) + [-v if det < 0 else v for v in q[2:3]] for q in p["pts"]]
    if p.get("loops"):
        out["loops"] = [[at(q) for q in loop] for loop in p["loops"]]
    if "r" in p:
        out["r"] = p["r"] * math.sqrt(abs(det))
    return out


def moved_ext(ext, m: tuple) -> list[float]:
    a, b, c, d = m
    xs = [a * x + c * y for x in (ext[0], ext[2]) for y in (ext[1], ext[3])]
    ys = [b * x + d * y for x in (ext[0], ext[2]) for y in (ext[1], ext[3])]
    return [min(xs), min(ys), max(xs), max(ys)]


CLASS_LABEL = {"TCH_RECTSTAIR": "双跑楼梯", "TCH_LINESTAIR": "直线梯段", "TCH_PATH_ARRAY": "路径阵列", "TCH_WALL": "墙",
               "TCH_DRAWINGNAME": "图名标注", "TCH_MULTILEADER": "引出标注", "TCH_OPENING": "门窗", "TCH_COLUMN": "柱",
               "TCH_AXIS_LABEL": "轴号", "TCH_ARROW": "箭头引注", "TCH_ELEVATION": "标高标注", "TCH_TEXT": "单行文字"}
PART_LABEL = {"LINE": "线", "LWPOLYLINE": "多段线", "POLYLINE": "多段线", "CIRCLE": "圆", "ARC": "弧", "INSERT": "块参照",
              "HATCH": "填充", "SOLID": "实心块"}


# 墙分解出来有一条颜色 46 的线，是墙的基线（定位线），图面上不显示，天正3 也不导出
def is_wall_baseline(o: dict, p: dict) -> bool:
    return o["dxf"] == "TCH_WALL" and p["type"] == "LINE" and p.get("color") == 46


def layout_blocks(doc) -> dict[str, str]:
    """布局名（大写）→ 它的块名（大写）。原图和天正3 的 *Paper_Space 编号不一定相同，按布局名对。"""
    return {lay.name.upper(): lay.block_record.dxf.name.upper() for lay in doc.layouts}


def entity_text(e) -> str:
    t = e.dxftype()
    s = e.dxf.text if t in ("TEXT", "ATTRIB") else plain(e.text) if t == "MTEXT" else ""
    # 天正存盘时没存图形的对象，代理图形里只有一行类名占位字（如 "TDbSymbCoord (TCH_KERNAL)"），不是图面内容
    return "" if PLACEHOLDER.match(s.strip()) else s.strip()


def block_texts(doc) -> Counter:
    """全图各块定义里的文字（含块属性）按内容计数。标注的匿名块 *D 不算，它的字是算出来的。"""
    texts = Counter()
    for blk in doc.blocks:
        if blk.name.upper().startswith("*D"):
            continue
        for e in blk:
            for x in ([e, *e.attribs] if e.dxftype() == "INSERT" else [e]):
                s = entity_text(x)
                if s:
                    texts[s] += 1
    return texts


def visible_texts(doc) -> Counter:
    """图面上看得见的文字：从模型空间和各布局出发，块参照一路展开。"""
    texts = Counter()
    for lay in doc.layouts:
        for e in disassemble.recursive_decompose(lay):
            s = entity_text(e)
            if s:
                texts[s] += 1
    return texts


def dimension_count(doc) -> int:
    return sum(len(blk.query("DIMENSION")) for blk in doc.blocks)


def dimension_text(doc, dim) -> str:
    """天正3 里一个标注实际画出来的字：在它的匿名块 *D 里。"""
    blk = doc.blocks.get(dim.dxf.get("geometry", ""))
    if blk is None:
        return ""
    return "".join(plain(e.text) if e.dxftype() == "MTEXT" else norm(e.dxf.text) for e in blk.query("MTEXT TEXT"))


def proxy_handles(doc, names: list[str]) -> dict[str, str]:
    """原图里的代理对象：句柄 → 原类的 DXF 名。names 是 acad.dxf_class_names 按文件先后数出来的。"""
    out = {}
    for blk in doc.blocks:
        for e in blk.query("ACAD_PROXY_ENTITY"):
            i = (e.acdb_proxy_entity.get_first_value(91, 0) if e.acdb_proxy_entity else 0) - 500
            out[e.dxf.handle.upper()] = names[i] if 0 <= i < len(names) else ""
    return out


def referenced_blocks(doc) -> set[str]:
    """从模型空间和各布局出发、沿块参照能走到的块（大写）。只被孤立块引用的块同样看不见。"""
    children = defaultdict(set)
    for blk in doc.blocks:
        for e in blk.query("INSERT"):
            children[blk.name.upper()].add(base_name(e.dxf.name).upper())
    seen = {lay.block_record.dxf.name.upper() for lay in doc.layouts}
    todo = list(seen)
    while todo:
        for c in children[todo.pop()] - seen:
            seen.add(c)
            todo.append(c)
    return seen


def top_counts(doc) -> Counter:
    top = Counter()
    for lay in doc.layouts:
        for e in lay:
            top[e.dxftype()] += 1
    return top


def load(dwg: Path, classes: bool = False):
    """DWG → ezdxf 文档；classes=True 时连同类名表一起返回（见 acad.dxf_class_names）。"""
    dxf = acad.WORK / f"verify-{uuid.uuid4().hex[:12]}.dxf"
    try:
        doc = ezdxf.readfile(acad.dxfout(dwg, dxf))
        return (doc, acad.dxf_class_names(dxf)) if classes else doc
    finally:
        dxf.unlink(missing_ok=True)


def verify(raw: Path, out: Path, rel: str) -> dict:
    """一张图的三方核对。「问题」是内容上的不一致（对象、文字、尺寸值、原文），有一条就不通过；
    「几何差异」是线条在两条路里画法不同，照实列出，不算不通过。"""
    raw_doc, class_names = load(raw / rel, classes=True)
    doc = load(t3_path(out, rel))
    objs = load_record(record_path(out, rel))
    res: dict = {"图纸": rel, "天正对象": len(objs)}
    problems: list[str] = []

    left = sum(len(b.query("ACAD_PROXY_ENTITY")) for b in doc.blocks)
    proxies = proxy_handles(raw_doc, class_names)
    tch = {h for h, name in proxies.items() if name.upper().startswith("TCH_")}
    res["代理"] = [len(proxies), left]
    if left:
        problems.append(f"天正3 里还有 {left} 个代理对象")
    recorded = {o["h"].upper() for o in objs}
    res["对象齐全"] = [len(recorded & tch), len(tch)]
    if recorded != tch:
        problems.append(f"记录与原图的天正对象对不上：原图有记录没有 {len(tch - recorded)}，记录有原图没有 {len(recorded - tch)}")

    # 记录里的图元到天正3 的同一个块里去认领。块在天正3 里不一定还叫原名：
    # - 镜像插入的块，天正3 另出一份 原名_~F（字不跟着镜像），两份都算
    # - 动态块的匿名实例（*U37）天正3 里没有了，换成 原名_M（镜像直接做进块里）或 原名_~F。
    #   这种按「原图的块参照和天正3 的块参照落在同一处」来对，连同块内坐标差的那个变换
    layouts = layout_blocks(doc)
    raw_layouts = {block: layout for layout, block in layout_blocks(raw_doc).items()}
    refs = referenced_blocks(raw_doc)
    raw_refs = defaultdict(list)
    for b in raw_doc.blocks:
        for e in b.query("INSERT"):
            raw_refs[e.dxf.name.upper()].append((b.name.upper(), e))
    index: dict[str, list[tuple]] = {}
    spots: dict[tuple, dict] = {}

    def refs_at(which: str, block, ref) -> list:
        """块 block 里与块参照 ref 同图层、同插入点的块参照。which 区分原图和天正3（两边的块可以同名）。"""
        if (which, block.name) not in spots:
            grid = defaultdict(list)
            for t in block.query("INSERT"):
                grid[(math.floor(t.dxf.insert[0]), math.floor(t.dxf.insert[1]))].append(t)
            spots[which, block.name] = grid
        x, y = ref.dxf.insert[0], ref.dxf.insert[1]
        return [t for dx in (-1, 0, 1) for dy in (-1, 0, 1)
                for t in spots[which, block.name].get((math.floor(x) + dx, math.floor(y) + dy), ())
                if t.dxf.layer == ref.dxf.layer and close(t.dxf.insert[0], x) and close(t.dxf.insert[1], y)]

    def stand_ins(name: str, known: set[str]) -> list[tuple]:
        """原图的块 name 在天正3 里被哪些别的块顶替：(天正3 块名, 块内坐标的变换)。天正3 的句柄是重排过的，只能按位置对。"""
        found = {}
        for owner, ref in raw_refs.get(name, ()):
            target = layouts.get(raw_layouts[owner]) if owner in raw_layouts else owner
            # 原图里本来就叠在这儿的别的块参照，天正3 里照样在，不是顶替的
            stacked = {base_name(t.dxf.name).upper() for t in refs_at("原图", raw_doc.blocks.get(owner), ref)} - {name}
            for block in (b for b in doc.blocks if target and base_name(b.name).upper() == target):
                for t in refs_at("天正3", block, ref):
                    m = between(ref, t)
                    if m and t.dxf.name not in known and base_name(t.dxf.name).upper() not in stacked:
                        found[(t.dxf.name, m)] = None
        return list(found)

    def blocks_of(o) -> list[tuple]:
        """(块索引, 变换, 是不是顶替的块)。同一个天正3 块顶替原图好几个块时各建各的索引，认领互不相干。"""
        key = "布局:" + o["layout"].upper() if o.get("layout") else o["owner"].upper()
        if key not in index:
            if o.get("layout"):
                name = layouts.get(o["layout"].upper())
                index[key] = [(BlockIndex(b), IDENTITY, False) for b in doc.blocks if name and b.name.upper() == name]
            else:
                named = [b for b in doc.blocks if base_name(b.name).upper() == base_name(o["owner"]).upper()]
                index[key] = [(BlockIndex(b), IDENTITY, False) for b in named] + [
                    (BlockIndex(doc.blocks.get(n)), m, True) for n, m in stand_ins(key, {b.name for b in named})]
        return index[key]

    tally = {k: [0, 0] for k in ("文字", "标注", "几何")}
    hidden = baselines = 0
    unexploded, dims_bad, dims_lost = [], [], []
    text_lost, geometry, differs = Counter(), Counter(), Counter()
    how, claimed, repeats = Counter(), Counter(), Counter()
    claimed_dims = repeat_dims = 0
    for o in objs:
        if "err" in o:
            unexploded.append([o["h"], o["dxf"], o["err"][:60]])
        visible = bool(o.get("layout")) or base_name(o["owner"]).upper() in refs
        blks = blocks_of(o)
        inner: dict[int, BlockIndex] = {}
        reach = 0.5 * max((p.get("h") or 0.0 for p in o.get("parts", []) if p["type"] in ("TEXT", "MTEXT")), default=0.0)
        for p in o.get("parts", []):
            if p["type"] == "!":
                unexploded.append([o["h"], p.get("dxf") or o["dxf"], p.get("err", "")[:60]])
                continue
            if is_wall_baseline(o, p):
                baselines += 1
                continue
            shape = part_shape(p)
            if shape is None:
                continue
            group = "文字" if shape[0] in ("TEXT", "MTEXT") else "标注" if p["type"] == "DIMENSION" else "几何"
            # 多出来的那几份块（_~F）里，这个对象的字、标注还会各出现一次
            if group == "文字":
                repeats[shape[1].strip()] += max(len(blks) - 1, 0)
            elif group == "标注":
                repeat_dims += max(len(blks) - 1, 0)
            hit, way = None, ""
            for n, (blk, m, stand_in) in enumerate(blks):
                q, shape_q = (p, shape) if m == IDENTITY else (moved(p, m), part_shape(moved(p, m)))
                hit, way = match_part(blk, q, shape_q, reach), "直接"
                if hit is None and "ext" in o and group == "几何":
                    if n not in inner:
                        inner[n] = blk.inside(o["ext"] if m == IDENTITY else moved_ext(o["ext"], m))
                    hit, way = match_part(inner[n], q, shape_q), "块参照展开后"
                if hit is None and group == "标注":
                    hit, way = exploded_dimension_text(blk, q), "标注已分解成线和字"
                if hit is not None:
                    way += "(顶替原块的块里)" if stand_in else ""
                    break
            if hit is None and not visible:
                hidden += 1
                continue
            tally[group][1] += 1
            if hit is None:
                if group == "文字":
                    text_lost[p["text"]] += 1
                elif group == "标注":
                    dims_lost.append([o["h"], base_name(o["owner"]), shown_measurement(p)])
                else:
                    geometry[f"{CLASS_LABEL.get(o['dxf'], o['dxf'])}的{PART_LABEL.get(p['type'], p['type'])}"] += 1
                    differs[o["h"]] += 1
                continue
            tally[group][0] += 1
            how[way if hit is not True else way + "(线段覆盖)"] += 1
            if hit is not True:
                claimed[entity_text(hit)] += 1
            # 分解成线和字的标注，那条字就是按显示值找到的，值已经对上；原样导成标注的，比它画出来的字
            if group == "标注" and way.startswith("直接"):
                claimed_dims += 1
                want, got = plain(shown_measurement(p)), dimension_text(doc, hit)
                if want != got:
                    dims_bad.append([o["h"], want, got])
    res["文字图元"], res["标注图元"], res["几何图元"] = tally["文字"], tally["标注"], tally["几何"]
    res["对上的方式"] = dict(how.most_common())
    res["几何差异"] = dict(geometry.most_common())
    res["几何差异对象"] = dict(differs.most_common(500))
    res["未引用块里未对上"] = hidden
    res["墙基线"] = baselines
    res["分解不了"] = unexploded[:20]
    res["尺寸值"] = [tally["标注"][0] - len(dims_bad), tally["标注"][0]]
    res["尺寸值不符"] = dims_bad[:12]
    res["天正3没导出的标注"] = dims_lost[:20]
    if text_lost:
        problems.append(f"记录里 {sum(text_lost.values())} 条文字在天正3 里找不到：{list(text_lost)[:5]}")
    if dims_bad:
        problems.append(f"{len(dims_bad)} 个尺寸值与天正3 画出来的不一样")

    # 原文拼得回
    ok = total = 0
    bad = []
    for o in objs:
        if not norm(expected_text(o)):
            continue
        total += 1
        hit = restore(o)
        if hit[0]:
            ok += 1
        else:
            bad.append([o["h"], o["cls"], norm(expected_text(o))[:40], hit[1][:40]])
    res["拼回"] = [ok, total]
    res["拼回失败样例"] = bad[:12]
    if bad:
        problems.append(f"{len(bad)} 个对象的原文拼不回")

    # 原图看得见的文字一条不少
    raw_seen, t3_seen = visible_texts(raw_doc), visible_texts(doc)
    lost = raw_seen - t3_seen
    res["可见文字"] = [sum(raw_seen.values()), sum(t3_seen.values())]
    res["丢失文字"] = sum(lost.values())
    res["丢失样例"] = list(lost.items())[:8]
    garbled = [sum(n for s, n in t.items() if GARBLED.search(s)) for t in (raw_seen, t3_seen)]
    res["乱码"] = garbled
    if lost:
        problems.append(f"原图 {sum(lost.values())} 条可见文字在天正3 里没有")
    if garbled[1]:
        problems.append(f"天正3 里 {garbled[1]} 条乱码")
    # 天正3 里的字，除了原图本来就有的、被记录认领走的，不该再有剩：剩下的就是天正导出时画了、分解时没画的
    claimed.pop("", None)
    extra = block_texts(doc) - claimed - block_texts(raw_doc) - repeats
    res["天正3多出文字"] = [sum(extra.values()), list(extra.items())[:12]]
    if extra:
        problems.append(f"天正3 多出 {sum(extra.values())} 条文字，记录里没有：{list(extra)[:5]}")
    # 标注同理：天正3 的标注数 = 原图的 + 被记录认领走的
    more_dims = dimension_count(doc) - dimension_count(raw_doc) - claimed_dims - repeat_dims
    res["天正3多出标注"] = max(more_dims, 0)
    if more_dims > 0:
        problems.append(f"天正3 多出 {more_dims} 个标注，记录里没有")

    raw_top, t3_top = top_counts(raw_doc), top_counts(doc)
    res["OLE"] = [raw_top["OLE2FRAME"], t3_top["OLE2FRAME"]]
    res["视口"] = [raw_top["VIEWPORT"], t3_top["VIEWPORT"]]
    for name, kind in (("OLE", "OLE2FRAME"), ("视口", "VIEWPORT")):
        if raw_top[kind] != t3_top[kind]:
            problems.append(f"{name} 数变了：{raw_top[kind]} → {t3_top[kind]}")
    res["问题"] = problems
    res["结论"] = "通过" if not problems else "不通过"
    return res


PAIRS = ("代理", "对象齐全", "文字图元", "标注图元", "几何图元", "尺寸值", "拼回", "乱码")


def summarize(rows: list[dict]) -> dict:
    done = [r for r in rows if "错误" not in r]
    total: dict = {"图纸": len(rows), "出错": len(rows) - len(done), "通过": sum(1 for r in done if r["结论"] == "通过"),
                   "天正对象": sum(r["天正对象"] for r in done)}
    for key in PAIRS:
        total[key] = [sum(r[key][0] for r in done), sum(r[key][1] for r in done)]
    geometry = Counter()
    for r in done:
        geometry.update(r["几何差异"])
    total.update({"丢失文字": sum(r["丢失文字"] for r in done), "天正3多出文字": sum(r["天正3多出文字"][0] for r in done),
                  "天正3多出标注": sum(r["天正3多出标注"] for r in done),
                  "天正3没导出的标注": sum(len(r["天正3没导出的标注"]) for r in done),
                  "分解不了": sum(len(r["分解不了"]) for r in done), "墙基线": sum(r["墙基线"] for r in done),
                  "几何差异": dict(geometry.most_common())})
    return total


def read_summary(out: Path) -> dict[str, dict]:
    last = {}
    path = out / SUMMARY
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                last[r["图纸"]] = r
    return last


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    # 有的原图句柄重复，ezdxf 每读一次刷一屏 WARNING，不影响核对
    logging.getLogger("ezdxf").setLevel(logging.ERROR)
    ap = argparse.ArgumentParser(description="原图、天正记录、天正3 三方逐张核对")
    ap.add_argument("raw", type=Path, help="原始图纸目录")
    ap.add_argument("out", type=Path, help="tarch_t3.py 的产物目录")
    ap.add_argument("--only", default="", help="只核对路径里含这些子串的图，逗号分隔")
    ap.add_argument("--redo", action="store_true", help="核对过的也重新核对")
    a = ap.parse_args()
    census = a.out / CENSUS
    if not census.exists():
        print(f"找不到 {census}，先跑 tarch_t3.py")
        return 1
    todo = [rel for rel in tarch_drawings(json.loads(census.read_text(encoding="utf-8"))) if matches(rel, a.only)]
    done = {} if a.redo else {rel: r for rel, r in read_summary(a.out).items() if "错误" not in r}
    failed = 0
    for n, rel in enumerate(todo, 1):
        if rel in done:
            continue
        t0 = time.time()
        try:
            res = verify(a.raw, a.out, rel)
        except Exception as ex:
            failed += 1
            res = {"图纸": rel, "错误": f"{type(ex).__name__}: {str(ex)[:300]}", "堆栈": traceback.format_exc()[-500:]}
        res["用时s"] = round(time.time() - t0, 1)
        with (a.out / SUMMARY).open("a", encoding="utf-8") as f:
            f.write(json.dumps(res, ensure_ascii=False) + "\n")
        print(f"[{n}/{len(todo)}] {res['用时s']}s {res.get('结论', '出错')} 对象{res.get('对象齐全')} 文字{res.get('文字图元')} "
              f"尺寸{res.get('尺寸值')} 拼回{res.get('拼回')} 几何{res.get('几何图元')} {'；'.join(res.get('问题', []))}"
              f"{res.get('错误', '')[:120]}  {rel}", flush=True)
    rows = [r for rel, r in read_summary(a.out).items() if rel in todo]
    total = summarize(rows)
    print(f"核对结束：{json.dumps(total, ensure_ascii=False)}", flush=True)
    return 1 if failed or total["通过"] != len(todo) else 0


if __name__ == "__main__":
    raise SystemExit(main())
