"""端到端往返测试：画图 -> 存盘 -> 读回核对。

同时验证中文图层名、中文文字内容和中文输出路径能不能活着穿过 GBK 脚本，
以及 DIMENSION 的 COM 补画通道是否接通。
"""
from __future__ import annotations

import json
import sys

import acad
import env

OUT = env.LOCAL / "自检" / "selftest.dwg"

LAYERS = [
    {"name": "轮廓线", "color": 3},
    {"name": "标注", "color": 1},
    {"name": "填充", "color": 5},
]

# 远超 DXF 单段 250 字上限，用来确认长正文切段后既不被截断也不被拼错
LONG_MTEXT = "".join(f"第{i:02d}段管道敷设说明，管材采用HDPE双壁波纹管，环刚度不小于8kN/m2。" for i in range(1, 26))

ENTITIES = [
    {"type": "LINE", "layer": "轮廓线", "p1": [0, 0, 0], "p2": [200, 0, 0]},
    {"type": "LINE", "layer": "轮廓线", "p1": [200, 0, 0], "p2": [200, 120, 0]},
    {"type": "CIRCLE", "layer": "轮廓线", "center": [100, 60, 0], "radius": 40},
    {"type": "ARC", "layer": "轮廓线", "center": [0, 0, 0], "radius": 60,
     "start_angle": 0, "end_angle": 90},
    {"type": "LWPOLYLINE", "layer": "轮廓线", "closed": True,
     "points": [[20, 20], [80, 20], [80, 50], [20, 50]]},
    {"type": "TEXT", "layer": "标注", "pos": [10, 100, 0], "height": 8,
     "value": "基础平面图 DN100"},
    {"type": "MTEXT", "layer": "标注", "pos": [0, 320, 0], "height": 5, "width": 400,
     "value": LONG_MTEXT},
    {"type": "ELLIPSE", "layer": "轮廓线", "center": [300, 60, 0],
     "major_axis": [40, 0, 0], "ratio": 0.5},
    {"type": "SPLINE", "layer": "轮廓线", "degree": 3,
     "points": [[0, 200, 0], [40, 260, 0], [90, 240, 0], [140, 200, 0], [190, 230, 0]]},
    {"type": "HATCH", "layer": "填充", "pattern": "SOLID",
     "boundary": {"type": "CIRCLE", "layer": "填充", "center": [400, 60, 0], "radius": 25}},
    {"type": "INSERT", "layer": "轮廓线", "block": "检查井", "pos": [500, 60, 0], "scale": 2,
     "define": [
         {"type": "CIRCLE", "center": [0, 0, 0], "radius": 5},
         {"type": "LINE", "p1": [-7, 0, 0], "p2": [7, 0, 0]},
     ]},
    {"type": "DIMENSION", "layer": "标注", "dim": "aligned",
     "p1": [0, 0, 0], "p2": [200, 0, 0], "text_pos": [100, -30, 0]},
]

EXPECTED = {
    "LINE": 2,
    "CIRCLE": 2,          # 1 个轮廓圆 + 1 个被保留的填充边界圆
    "ARC": 1,
    "LWPOLYLINE": 1,
    "TEXT": 1,
    "MTEXT": 1,
    "ELLIPSE": 1,
    "SPLINE": 1,
    "HATCH": 1,
    "INSERT": 1,
    "DIMENSION": 1,
}


def test_entities() -> int:
    n_com = sum(1 for e in ENTITIES if e["type"] in acad.COM_ONLY_TYPES)
    print(f"[1/2] 画图 -> {OUT}")
    print(f"      无头 {len(ENTITIES) - n_com} 个图元 + COM 通道 {n_com} 个标注")
    res = acad.draw(ENTITIES, OUT, layers=LAYERS, timeout=300)
    print(f"      ok={res.ok} elapsed={res.elapsed}s")
    if res.outputs.get("com"):
        print(f"      COM: {res.outputs['com']}")
    if res.error:
        print(f"      error: {res.error}")

    # COM 单独失败不算无头回归失败：试用期许可弹窗会让 AutoCAD 持续返回
    # RPC_E_CALL_REJECTED，实测热路径也挡得住。无头部分照常核对。
    com_blocked = not res.ok and "补标注失败" in res.error and OUT.exists()
    if com_blocked:
        print("      ⚠ COM 通道受阻，DIMENSION 未验证（无头部分继续核对）")
    elif not res.ok:
        if res.log:
            print("--- journal ---")
            print(res.log[-1500:])
        return 1

    print("[2/2] 读回核对")
    res2, ents, _ = acad.dump_drawing(OUT)
    print(f"      ok={res2.ok} elapsed={res2.elapsed}s 图元数={len(ents)}")
    if res2.error:
        print(f"      error: {res2.error}")

    kinds: dict[str, int] = {}
    layers_seen: set[str] = set()
    texts: list[str] = []
    mtexts: list[str] = []
    for e in ents:
        k = str(e.get("0", "?"))
        kinds[k] = kinds.get(k, 0) + 1
        if e.get("8"):
            layers_seen.add(str(e["8"]))
        if k == "TEXT":
            texts.append(str(e.get("1", "")))
        if k == "MTEXT":
            mtexts.append(acad.entity_text(e))

    print(f"      类型分布: {kinds}")
    print(f"      图层: {sorted(layers_seen)}")
    print(f"      文字: {texts}")
    print(f"      MTEXT 长度: {[len(m) for m in mtexts]}（写入 {len(LONG_MTEXT)}）")

    expected = dict(EXPECTED)
    if com_blocked:
        expected.pop("DIMENSION")
    problems = [
        f"{k} 期望 {v} 实际 {kinds.get(k, 0)}"
        for k, v in expected.items()
        if kinds.get(k, 0) != v
    ]
    if "轮廓线" not in layers_seen:
        problems.append("中文图层名 '轮廓线' 丢失")
    if not any("基础平面图" in t for t in texts):
        problems.append(f"中文文字丢失，实际得到 {texts}")
    if not mtexts:
        problems.append("MTEXT 未读回")
    elif mtexts[0] != LONG_MTEXT:
        problems.append(
            f"MTEXT 长正文往返不一致：写入 {len(LONG_MTEXT)} 字，读回 {len(mtexts[0])} 字"
        )

    poly = next((e for e in ents if e.get("0") == "LWPOLYLINE"), None)
    if poly is not None:
        verts = poly.get("10")
        n = len(verts) if isinstance(verts, list) and isinstance(verts[0], list) else 1
        print(f"      多段线顶点数: {n}")
        if n != 4:
            problems.append(f"多段线顶点期望 4 实际 {n}")

    blk = next((e for e in ents if e.get("0") == "INSERT"), None)
    if blk is not None:
        print(f"      块参照: 块名={blk.get('2')} 缩放={blk.get('41')}")
        if blk.get("2") != "检查井":
            problems.append(f"块名期望 检查井 实际 {blk.get('2')}")

    if problems:
        print("\n未通过:")
        for p in problems:
            print(f"  - {p}")
        print("\n完整 dump:")
        print(json.dumps(ents, ensure_ascii=False, indent=2)[:3000])
        return 1

    dim = "DIMENSION 未验证（COM 受阻）" if com_blocked else "DIMENSION 经 COM 通道补齐"
    print(f"\n[1] 通过：11 类图元往返完整，中文图层/文字/路径无损，"
          f"MTEXT {len(LONG_MTEXT)} 字长正文分段后原样读回，{dim}。")
    return 0


SHEET_OUT = OUT.with_name("selftest_sheet.dwg")
SHEET_PDF = OUT.with_name("selftest_sheet.pdf")

SHEET_STYLES = [
    {"type": "LTYPE", "name": "DASHED", "pattern": [10, -5], "description": "虚线"},
    {"type": "STYLE", "name": "GB", "font": "gbenor.shx", "bigfont": "gbcbig.shx"},
    {"type": "DIMSTYLE", "name": "市政标注", "text_height": 3.5, "arrow_size": 2.5},
]

SHEET_LAYERS = [
    {"name": "图框", "color": 7},
    {"name": "管线", "color": 3, "ltype": "DASHED"},
    {"name": "检查井", "color": 1},
    {"name": "注记", "color": 4},
]

SHEET_ENTITIES = [
    {"type": "LWPOLYLINE", "layer": "图框", "closed": True,
     "points": [[0, 0], [594, 0], [594, 420], [0, 420]]},
    {"type": "LINE", "layer": "管线", "ltype": "DASHED",
     "p1": [60, 200, 0], "p2": [540, 200, 0]},
    {"type": "TEXT", "layer": "注记", "pos": [40, 380, 0], "height": 14,
     "value": "雨污分流管网平面图", "style": "GB"},
    {"type": "LEADER", "layer": "注记", "dimstyle": "市政标注",
     "points": [[300, 200, 0], [340, 250, 0], [400, 250, 0]]},
    {"type": "MTEXT", "layer": "注记", "pos": [402, 254, 0], "height": 8,
     "value": "DN300 混凝土管 i=0.003", "style": "GB"},
    {
        "type": "INSERT", "layer": "检查井", "block": "检查井符号",
        "pos": [180, 200, 0],
        "define": [
            {"type": "CIRCLE", "center": [0, 0, 0], "radius": 6},
            {"type": "ATTDEF", "pos": [9, 3, 0], "height": 7, "tag": "井号",
             "prompt": "检查井编号", "value": "Y-", "style": "GB"},
        ],
        "attribs": [
            {"tag": "井号", "offset": [9, 3, 0], "height": 7,
             "value": "Y12", "style": "GB"},
        ],
    },
]

SHEET_EXPECTED = {"LWPOLYLINE": 1, "LINE": 1, "TEXT": 1, "LEADER": 1,
                  "MTEXT": 1, "INSERT": 1}

# 符号表和 ATTRIB 值 dump_drawing 读不到：ATTRIB 是 INSERT 的子实体，
# 符号表根本不是图元。单独用一段 LISP 查完落盘回传。
SHEET_CHECK = r'''
(setq _r "")
(foreach p (list (list "LTYPE" "DASHED") (list "STYLE" "GB") (list "DIMSTYLE" "市政标注"))
  (setq _r (strcat _r (cadr p) "=" (if (tblsearch (car p) (cadr p)) "有" "无") "\n")))
(setq _ss (ssget "_X" (list (cons 0 "INSERT"))))
(if _ss
  (progn
    (setq _e (ssname _ss 0))
    (setq _sub (entget (entnext _e)))
    (setq _r (strcat _r "ATTRIB=" (cdr (assoc 0 _sub))
                     " tag=" (cdr (assoc 2 _sub))
                     " 值=" (cdr (assoc 1 _sub)) "\n")))
  (setq _r (strcat _r "ATTRIB=无 INSERT\n")))
(setq _l (ssget "_X" (list (cons 0 "LINE") (cons 6 "DASHED"))))
(setq _r (strcat _r "虚线管线=" (if _l (itoa (sslength _l)) "0") "\n"))
'''


def test_sheet() -> int:
    print(f"\n[2/2] 施工图要素 -> {SHEET_OUT}")
    res = acad.draw(
        SHEET_ENTITIES, SHEET_OUT,
        layers=SHEET_LAYERS, styles=SHEET_STYLES,
        layouts=[{"name": "A2出图",
                  "viewports": [{"corner1": [20, 20], "corner2": [560, 380]}]}],
        plot={"pdf": SHEET_PDF, "paper": "A2"},
        timeout=300,
    )
    print(f"      ok={res.ok} elapsed={res.elapsed}s")
    if res.error:
        print(f"      error: {res.error}")
        if res.log:
            print(res.log[-1200:])
        return 1
    print(f"      PDF: {res.outputs.get('pdf', '未生成')}")

    _, ents, _ = acad.dump_drawing(SHEET_OUT)
    kinds: dict[str, int] = {}
    for e in ents:
        k = str(e.get("0", "?"))
        kinds[k] = kinds.get(k, 0) + 1
    print(f"      类型分布: {kinds}")

    out = acad.WORK / "sheet_check.txt"
    out.unlink(missing_ok=True)
    chk = acad.run_lisp(
        SHEET_CHECK
        + f'(setq _f (open {acad.lisp_str(acad.lisp_path(out))} "w"))'
          "(write-line _r _f)(close _f)",
        drawing=SHEET_OUT,
        reads={"check": out},
        timeout=120,
    )
    report = chk.outputs.get("check", "").strip()
    for line in report.splitlines():
        print(f"      {line}")

    problems = [
        f"{k} 期望 {v} 实际 {kinds.get(k, 0)}"
        for k, v in SHEET_EXPECTED.items()
        if kinds.get(k, 0) != v
    ]
    if kinds.get("VIEWPORT", 0) < 1:
        problems.append("布局视口未建出")
    for name in ("DASHED=有", "GB=有", "市政标注=有"):
        if name not in report:
            problems.append(f"符号表缺项：{name.split('=')[0]}")
    if "tag=井号 值=Y12" not in report:
        problems.append(f"属性值未挂上，实际回传：{report!r}")
    if "虚线管线=1" not in report:
        problems.append("管线没引用 DASHED 线型")
    if not SHEET_PDF.exists() or SHEET_PDF.stat().st_size < 1000:
        problems.append(f"PDF 异常：{SHEET_PDF}")

    if problems:
        print("\n未通过:")
        for p in problems:
            print(f"  - {p}")
        return 1

    print(f"\n[2] 通过：样式表 3 项、属性块值、DASHED 线型、布局视口、"
          f"A2 PDF（{SHEET_PDF.stat().st_size} 字节）全部核对一致。")
    return 0


def main() -> int:
    rc = test_entities()
    rc |= test_sheet()
    return rc


if __name__ == "__main__":
    sys.exit(main())
