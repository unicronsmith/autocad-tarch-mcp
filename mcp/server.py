"""AutoCAD MCP server。

把 acad.py 的无头 AutoLISP 驱动包成 MCP 工具。真实图纸动辄几千图元，全量
回传会撑爆调用方上下文，所以读图默认只回摘要，完整数据落盘成 JSON。
"""
from __future__ import annotations

import json
import sys
import warnings
from csv import writer as csv_writer
from datetime import datetime
from pathlib import Path
from typing import Any

warnings.filterwarnings("ignore", category=UserWarning, module="pydantic_settings")

sys.path.insert(0, str(Path(__file__).resolve().parent))

import acad
import env
from mcp.server.fastmcp import FastMCP

ROOT = env.ROOT
RAW = ROOT / "RAW"
RECOGNIZED = ROOT / "图纸识别产物"
DRAWN = ROOT / "画图产物"

mcp = FastMCP("autocad")

CODE_NAMES = {
    "0": "type", "1": "text", "2": "name", "5": "handle", "6": "linetype",
    "7": "textstyle", "8": "layer", "10": "point", "11": "point2",
    "38": "elevation", "39": "thickness", "40": "size", "41": "scale",
    "50": "angle_start", "51": "angle_end", "62": "color", "70": "flags",
    "90": "vertex_count", "210": "extrusion", "410": "space",
}


def _humanize(ent: dict) -> dict:
    out: dict[str, Any] = {}
    for code, val in ent.items():
        name = CODE_NAMES.get(code, f"dxf{code}")
        if code == "1":
            raw = acad.entity_text(ent)
            cleaned = acad.clean_text(raw)
            out[name] = cleaned
            if cleaned != raw:
                out["text_raw"] = raw
        elif code == "3":
            continue
        else:
            out[name] = val
    return out


def _resolve(path: str) -> Path:
    p = Path(path)
    if not p.is_absolute():
        for base in (RAW, ROOT, DRAWN):
            if (base / p).exists():
                return base / p
        return ROOT / p
    return p


def _summarize(ents: list[dict], text_limit: int = 150) -> dict:
    kinds: dict[str, int] = {}
    layers: dict[str, int] = {}
    blocks: dict[str, int] = {}
    texts: list[str] = []
    dims: list[str] = []
    xs: list[float] = []
    ys: list[float] = []
    for e in ents:
        k = str(e.get("0", "?"))
        kinds[k] = kinds.get(k, 0) + 1
        lay = str(e.get("8", "")) or "0"
        layers[lay] = layers.get(lay, 0) + 1
        if k == "INSERT" and e.get("2"):
            blocks[str(e["2"])] = blocks.get(str(e["2"]), 0) + 1
        elif k == "DIMENSION" and e.get("1"):
            dims.append(str(e["1"]))
        elif k in ("TEXT", "MTEXT") and (e.get("1") or e.get("3")):
            t = acad.clean_text(acad.entity_text(e))
            if t:
                texts.append(t)
        pt = e.get("10")
        pts = pt if isinstance(pt, list) and pt and isinstance(pt[0], list) else [pt]
        for p in pts:
            if isinstance(p, list) and len(p) >= 2:
                xs.append(p[0])
                ys.append(p[1])

    # 图框、编号这类文字会重复成百上千条，去重后才看得出图纸讲了什么
    seen: set[str] = set()
    uniq: list[str] = []
    for t in texts:
        key = t.strip()
        if key and key not in seen:
            seen.add(key)
            uniq.append(t if len(t) <= 120 else t[:120] + "…")

    extent = None
    if xs and ys:
        extent = {
            "min": [round(min(xs), 3), round(min(ys), 3)],
            "max": [round(max(xs), 3), round(max(ys), 3)],
        }
    out: dict[str, Any] = {
        "总图元数": len(ents),
        "类型分布": dict(sorted(kinds.items(), key=lambda kv: -kv[1])),
        "图层分布": dict(sorted(layers.items(), key=lambda kv: -kv[1])[:30]),
        "图层总数": len(layers),
        "文字条数": len(texts),
        "文字去重后": len(uniq),
        "文字内容": uniq[:text_limit],
        "范围": extent,
    }
    if len(uniq) > text_limit:
        out["文字说明"] = f"去重后共 {len(uniq)} 条，仅列前 {text_limit} 条，完整内容见落盘 JSON"
    if blocks:
        out["块引用"] = dict(sorted(blocks.items(), key=lambda kv: -kv[1])[:20])
    if dims:
        out["标注值"] = dims[:60]
    return out


def _save(stem: str, suffix: str, data: Any) -> Path:
    RECOGNIZED.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    p = RECOGNIZED / f"{stem}-{suffix}-{stamp}.json"
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def _fail(res) -> str:
    return json.dumps(
        {"错误": res.error or "执行失败", "耗时秒": res.elapsed,
         "日志尾部": (res.log or "")[-800:]},
        ensure_ascii=False,
    )


@mcp.tool()
def read_drawing(
    path: str,
    mode: str = "survey",
    filter_type: str = "",
    filter_layer: str = "",
    limit: int = 100,
    max_entities: int = 20000,
) -> str:
    """读取 DWG/DXF 图纸。每次调用无头启动一个 AutoCAD，小图约 2 秒。

    先用 survey 摸清规模和图层构成，再决定要不要取明细 —— 工程图动辄几万到
    几十万图元，一张 50MB 的地形图有近 50 万个，全量取回没有意义。

    Args:
        path: 图纸路径，相对路径会依次在 RAW / 项目根 / 画图产物 下查找
        mode: survey 只统计规模、图层、块引用、范围（任意大小都能跑）；
              text 只抓文字并清洗格式码，最适合"读懂这张图在讲什么"；
              full 取图元明细，务必配合 filter 收窄
        filter_type: 图元类型，逗号分隔，如 "TEXT,MTEXT" 或 "LINE"，支持通配符
        filter_layer: 图层名，逗号分隔，支持通配符，如 "给水*"
        limit: 回传给调用方的条数上限（完整数据始终落盘）
        max_entities: full 模式在 AutoCAD 侧的截断上限，防止导出几百 MB
    """
    dwg = _resolve(path)
    if not dwg.exists():
        return json.dumps({"错误": f"图纸不存在: {dwg}"}, ensure_ascii=False)

    if mode == "survey":
        res, info = acad.survey_drawing(dwg)
        if not res.ok:
            return _fail(res)
        payload = {"图纸": str(dwg), "耗时秒": res.elapsed}
        payload["总图元数"] = info["总图元数"]
        payload["类型分布"] = info["类型分布"]
        payload["图层总数"] = len(info["图层分布"])
        payload["图层分布"] = dict(list(info["图层分布"].items())[:40])
        if info["块引用"]:
            payload["块引用种类"] = len(info["块引用"])
            payload["块引用"] = dict(list(info["块引用"].items())[:30])
        payload["范围"] = info["范围"]
        payload["提示"] = "要读文字用 mode='text'；要几何明细用 mode='full' 并加 filter_type/filter_layer"
        proxies = info["类型分布"].get("ACAD_PROXY_ENTITY", 0)
        if proxies:
            payload["代理对象"] = f"顶层有 {proxies} 个代理对象（自定义对象，这里读不出内容）。是天正图就用 read_tarch 识别"
        return json.dumps(payload, ensure_ascii=False, indent=2)

    if mode == "text":
        res, items = acad.grab_text(dwg)
        if not res.ok:
            return _fail(res)
        saved = _save(dwg.stem, "text", items)
        seen: set[str] = set()
        uniq: list[dict] = []
        for it in items:
            key = it["文字"].strip()
            if key and key not in seen:
                seen.add(key)
                uniq.append(it)
        by_layer: dict[str, int] = {}
        for it in items:
            by_layer[it["图层"]] = by_layer.get(it["图层"], 0) + 1
        payload = {
            "图纸": str(dwg),
            "耗时秒": res.elapsed,
            "完整数据": str(saved),
            "文字条数": len(items),
            "去重后": len(uniq),
            "按图层": dict(sorted(by_layer.items(), key=lambda kv: -kv[1])[:25]),
            "文字": [it["文字"] for it in uniq[:limit]],
        }
        if len(uniq) > limit:
            payload["说明"] = f"去重后 {len(uniq)} 条，仅回传前 {limit} 条，其余见完整数据"
        return json.dumps(payload, ensure_ascii=False, indent=2)

    res, ents, matched = acad.dump_drawing(
        dwg, types=filter_type, layers=filter_layer, max_entities=max_entities
    )
    if not res.ok:
        return _fail(res)
    saved = _save(dwg.stem, "dump", [_humanize(e) for e in ents])
    payload = {
        "图纸": str(dwg),
        "耗时秒": res.elapsed,
        "完整数据": str(saved),
        "匹配总数": matched,
        "已导出": len(ents),
        "摘要": _summarize(ents),
        "明细": [_humanize(e) for e in ents[:limit]],
    }
    if matched > len(ents):
        payload["截断"] = f"匹配 {matched} 条，按 max_entities={max_entities} 截断为 {len(ents)} 条"
    if len(ents) > limit:
        payload["明细说明"] = f"已导出 {len(ents)} 条，仅回传前 {limit} 条，其余见完整数据"
    return json.dumps(payload, ensure_ascii=False, indent=2)


@mcp.tool()
def read_tarch(path: str, limit: int = 80, check: bool = True) -> str:
    """识别天正图：含天正自定义对象（墙、门窗、柱、尺寸标注、引注、标高、管线……）的 DWG。

    天正对象在 read_drawing 里只是读不出内容的 ACAD_PROXY_ENTITY。这个工具调本机的天正建筑：先在天正
    进程里把每个对象分解一遍、读一遍属性，再导出天正3，最后整理成识别结果。第一次处理一张图要拉起
    天正 AutoCAD（屏幕上会出现它的窗口，会以管理员身份运行；实测一张 608 个对象的图连核对 35 秒）；
    处理过的直接读缓存。
    不含天正对象的图会直接告诉你，改用 read_drawing。

    回传的是摘要：各类对象数、汇总表（门窗表、墙、柱、房间、管线、图名、标高、轴号、尺寸值）、图面文字。
    每个对象的属性和完整几何在落盘的 JSON 里，按句柄查。

    Args:
        path: 图纸路径，相对路径会依次在 RAW / 项目根 / 画图产物 下查找
        limit: 回传的文字条数上限（完整数据始终落盘）
        check: 是否同时做原图 / 记录 / 天正3 三方核对。「结论」只看内容（对象齐全、文字、尺寸值、原文、不丢字不多字）；
            「几何差异」是分解出的线条和图面显示不一样的地方，这些对象在识别结果里带「与天正3不同的几何图元数」
    """
    import contextlib
    import hashlib
    import io
    import logging

    import tarch_recognize
    import tarch_t3
    import tarch_verify

    dwg = _resolve(path).resolve()
    if not dwg.exists():
        return json.dumps({"错误": f"图纸不存在: {dwg}"}, ensure_ascii=False)
    raw, rel = dwg.parent, dwg.name
    out = RECOGNIZED / "天正" / f"{raw.name}-{hashlib.md5(str(raw).lower().encode('utf-8')).hexdigest()[:6]}"
    logging.getLogger("ezdxf").setLevel(logging.ERROR)
    # 转换过程往 stdout 打进度，而 stdout 是 MCP 的通信管道，得接走
    log = io.StringIO()
    try:
        with contextlib.redirect_stdout(log):
            code = tarch_t3.convert(raw, out, files=[rel])
        census = json.loads((out / tarch_verify.CENSUS).read_text(encoding="utf-8")).get(rel, {})
        if "错误" in census:
            return json.dumps({"错误": f"打不开这张图: {census['错误']}"}, ensure_ascii=False)
        if not any(k.startswith("TCH_") for k in census.get("代理", {})):
            return json.dumps({"图纸": str(dwg), "天正对象": 0, "其它代理对象": census.get("代理", {}),
                               "说明": "这张图没有天正对象，用 read_drawing 读"}, ensure_ascii=False)
        if code or not tarch_verify.record_path(out, rel).exists():
            return json.dumps({"错误": "天正转换没成功", "日志尾部": log.getvalue()[-1500:]}, ensure_ascii=False)
        v = None
        if check:
            with contextlib.redirect_stdout(log):
                v = tarch_verify.verify(raw, out, rel)
        result = tarch_recognize.recognize_file(out, rel, v)
        payload: dict[str, Any] = {
            "图纸": str(dwg),
            "天正对象": result["天正对象"],
            "按类": result["按类"],
            "汇总": result["汇总"],
            "识别结果": str(tarch_recognize.recognized_path(out, rel)),
            "对象记录（完整几何和属性）": str(tarch_verify.record_path(out, rel)),
            "天正3": str(tarch_verify.t3_path(out, rel)),
        }
        seen: set[str] = set()
        texts = [t for it in result["对象"] for t in it.get("文字", []) if not (t in seen or seen.add(t))]
        payload["文字去重后"] = len(texts)
        payload["文字"] = texts[:limit]
        if len(texts) > limit:
            payload["文字说明"] = f"去重后 {len(texts)} 条，仅回传前 {limit} 条，其余见识别结果"
        if v:
            payload["核对"] = {k: v[k] for k in ("结论", "问题", "对象齐全", "文字图元", "尺寸值", "拼回", "丢失文字", "乱码", "几何图元", "几何差异")}
        payload["提示"] = "天正对象以外的内容（普通文字、线、块）用 read_drawing 读「天正3」那个文件"
        return json.dumps(payload, ensure_ascii=False, indent=2)
    except Exception as exc:
        return json.dumps({"错误": f"{type(exc).__name__}: {exc}", "日志尾部": log.getvalue()[-1500:]}, ensure_ascii=False)


@mcp.tool()
def draw(
    entities: list[dict],
    out_name: str,
    layers: list[dict] | None = None,
    styles: list[dict] | None = None,
    layouts: list[dict] | None = None,
    plot: dict | None = None,
) -> str:
    """造图元并保存为 DWG，可同时出 PDF。

    每次调用会无头启动一个 AutoCAD 实例。只有列表里含 DIMENSION 时才会额外
    连 GUI 实例补标注（无头内核不执行标注命令），那种情况下会慢几秒。

    Args:
        entities: 图元列表。按 type 取不同字段：
            LINE        p1, p2            各为 [x,y,z]
            CIRCLE      center, radius
            ARC         center, radius, start_angle, end_angle   角度单位为度
            ELLIPSE     center, major_axis, ratio, start_param/end_param(可选)
            TEXT        pos, height, value, rotation(可选)
            MTEXT       pos, height, width, value, style/attachment/rotation(可选)
                        value 里用 \\P 换行；超 250 字会自动切段，不会被截断
            LWPOLYLINE  points(二维点列表), closed(布尔)
            SPLINE      points(控制点列表), degree(默认 3), closed(布尔)
            HATCH       boundary(一个闭合图元描述，如 CIRCLE/LWPOLYLINE),
                        pattern(默认 SOLID), scale, angle, keep_boundary(默认 True)
            INSERT      block(块名), pos, scale(数或[sx,sy,sz]), rotation(可选),
                        define(可选，子图元列表；给了就先造块定义), base(可选块基点),
                        attribs(可选，属性值列表 [{tag, value, offset 或 pos, height}]；
                        offset 是相对插入点的偏移，pos 是 WCS 绝对坐标)
            ATTDEF      pos, height, tag, prompt(可选), value(默认值), 只放在 define 里
            LEADER      points(引线折点列表), dimstyle(可选，决定箭头大小)
            MULTILEADER value；几何由 AutoCAD 补默认值，要控制位置请用 LEADER
            DIMENSION   dim=aligned   p1, p2, text_pos
                        dim=linear    p1, p2, text_pos, angle(弧度，默认 0)
                        dim=radial    center, chord_point, leader
                        dim=diametric chord_point, far_chord_point, leader
                        dim=angular   vertex, p1, p2, text_pos
                        text(可选，覆盖标注文字)
            通用可选字段: layer, color(1红 2黄 3绿 4青 5蓝 6洋红 7黑白),
                         ltype(线型名，要先在 styles 里建), style(TEXT/MTEXT 的文字样式)
        out_name: 输出文件名或路径，相对路径落在 画图产物 下
        layers: 要先建的图层，如 [{"name":"轮廓线","color":3,"ltype":"DASHED"}]
        styles: 先建的样式表，按 type 分：
            LTYPE     name, pattern(划长列表，正画线负留空), description
            STYLE     name, font, bigfont(中文字形靠它，默认 gbcbig.shx), height
            DIMSTYLE  name, text_height, arrow_size, scale, decimals
        layouts: 图纸空间布局，如
            [{"name":"A2出图","viewports":[{"corner1":[20,20],"corner2":[560,380]}],
              "entities":[...图框等只在布局里出现的图元]}]
        plot: 给了就在存盘后出 PDF，如 {"pdf":"x.pdf","paper":"A2"}。
            paper 可写 A0/A1/A2/A3/A4 或设备支持的完整尺寸名；
            area 默认 E(图形范围)，scale 默认 F(布满)，ctb 默认 "."(无样式表)
    """
    out = Path(out_name)
    if not out.is_absolute():
        out = DRAWN / out
    if out.suffix.lower() not in (".dwg", ".dxf"):
        out = out.with_suffix(".dwg")

    if plot and not Path(plot.get("pdf", "")).is_absolute():
        plot = dict(plot, pdf=str(DRAWN / plot.get("pdf", out.with_suffix(".pdf").name)))

    try:
        res = acad.draw(
            entities, out, layers=layers, styles=styles, layouts=layouts, plot=plot
        )
    except acad.AcadError as exc:
        return json.dumps({"错误": str(exc)}, ensure_ascii=False)

    if not res.ok:
        return json.dumps(
            {"错误": res.error, "耗时秒": res.elapsed, "日志尾部": res.log[-800:]},
            ensure_ascii=False,
        )
    payload = {"已生成": str(out), "图元数": len(entities), "耗时秒": res.elapsed}
    if res.outputs.get("com"):
        payload["COM 通道"] = res.outputs["com"]
    if res.outputs.get("pdf"):
        payload["PDF"] = res.outputs["pdf"]
    return json.dumps(payload, ensure_ascii=False)


@mcp.tool()
def run_autolisp(code: str, drawing: str = "", save_as: str = "") -> str:
    """执行任意 AutoLISP 代码（逃生舱，用于上面两个工具覆盖不到的操作）。

    代码可以自由换行，会被自动压成单行送进 AutoCAD 命令行。想取回数据就在
    代码里用 (open ...) / (write-line ...) 写文件，然后自己读那个文件。

    Args:
        code: AutoLISP 源码
        drawing: 要打开的图纸，留空则在新建的空白图纸上执行
        save_as: 执行完另存为的路径，相对路径落在 画图产物 下
    """
    dwg = _resolve(drawing) if drawing else None
    if dwg is not None and not dwg.exists():
        return json.dumps({"错误": f"图纸不存在: {dwg}"}, ensure_ascii=False)

    post = None
    out = None
    if save_as:
        out = Path(save_as)
        if not out.is_absolute():
            out = DRAWN / out
        if out.suffix.lower() not in (".dwg", ".dxf"):
            out = out.with_suffix(".dwg")
        out.parent.mkdir(parents=True, exist_ok=True)
        code = '(setvar "FILEDIA" 0)\n' + code
        post = ["_.SAVEAS", "", f'"{acad.lisp_path(out)}"']

    try:
        res = acad.run_lisp(code, drawing=dwg, post_lines=post)
    except acad.AcadError as exc:
        return json.dumps({"错误": str(exc)}, ensure_ascii=False)

    payload: dict[str, Any] = {"成功": res.ok, "耗时秒": res.elapsed}
    if res.error:
        payload["错误"] = res.error
    if not res.ok and res.log:
        payload["日志尾部"] = res.log[-800:]
    if out is not None:
        payload["已生成"] = str(out) if out.exists() else f"未生成 {out}"
    return json.dumps(payload, ensure_ascii=False, indent=2)


@mcp.tool()
def list_attributes(path: str) -> str:
    """列出图纸里真正挂了扩展数据(XDATA)的属性名及其对象数。

    市政管网图常把普查属性挂在普通图元上 —— 管径、管线材料、起点埋深、
    井底高程、起始点/终止点编号等。先用这个看有哪些属性可取，再用
    extract_attributes 按某个属性名把对象连同全部属性导出来。

    Args:
        path: 图纸路径，相对路径会依次在 RAW / 项目根 / 画图产物 下查找
    """
    dwg = _resolve(path)
    if not dwg.exists():
        return json.dumps({"错误": f"图纸不存在: {dwg}"}, ensure_ascii=False)
    res, apps = acad.probe_xdata_apps(dwg)
    if not res.ok:
        return _fail(res)
    return json.dumps(
        {
            "图纸": str(dwg),
            "耗时秒": res.elapsed,
            "有数据的属性数": len(apps),
            "属性": apps,
            "提示": "用 extract_attributes(path, anchor='管径') 把带该属性的对象连同全部属性导出",
        },
        ensure_ascii=False,
        indent=2,
    )


@mcp.tool()
def extract_attributes(
    path: str,
    anchor: str,
    limit: int = 30,
    max_entities: int = 5000,
    csv: bool = True,
) -> str:
    """把带 anchor 属性的图元连同其全部扩展数据导出为台账。

    典型用法：anchor='管径' 导出全部管线（含材质、埋深、起终点编号、所属
    道路和两端坐标），anchor='井底高程' 导出全部检查井。导出的是对象的完整
    属性集，不只是 anchor 那一项。

    Args:
        path: 图纸路径
        anchor: 用来筛选对象的属性名，先用 list_attributes 查看可选值
        limit: 回传给调用方的行数上限（完整数据始终落盘）
        max_entities: 在 AutoCAD 侧的截断上限
        csv: 同时导出 CSV，方便直接用 Excel 打开
    """
    dwg = _resolve(path)
    if not dwg.exists():
        return json.dumps({"错误": f"图纸不存在: {dwg}"}, ensure_ascii=False)

    res, rows, matched = acad.extract_xdata(dwg, anchor, max_entities=max_entities)
    if not res.ok:
        return _fail(res)
    if not rows:
        return json.dumps(
            {"图纸": str(dwg), "匹配": 0, "说明": f"没有图元挂着属性 {anchor!r}"},
            ensure_ascii=False,
        )

    saved = _save(dwg.stem, f"attr-{anchor}", rows)
    payload: dict[str, Any] = {
        "图纸": str(dwg),
        "耗时秒": res.elapsed,
        "锚点属性": anchor,
        "匹配总数": matched,
        "已导出": len(rows),
        "完整数据": str(saved),
    }

    if csv:
        cols: list[str] = []
        for r in rows:
            for k in r:
                if k not in cols:
                    cols.append(k)
        csv_path = saved.with_suffix(".csv")
        with csv_path.open("w", encoding="utf-8-sig", newline="") as fh:
            w = csv_writer(fh)
            w.writerow(cols)
            for r in rows:
                w.writerow([r.get(c, "") for c in cols])
        payload["CSV"] = str(csv_path)
        payload["字段数"] = len(cols)

    # 字段多达几十个，全回传会淹没调用方，只给填充率高的那些
    fill: dict[str, int] = {}
    for r in rows:
        for k in r:
            fill[k] = fill.get(k, 0) + 1
    useful = [k for k, v in sorted(fill.items(), key=lambda kv: -kv[1]) if v >= len(rows) * 0.5]
    payload["常用字段"] = useful
    payload["样本"] = [{k: r[k] for k in useful if k in r} for r in rows[:limit]]
    if matched > len(rows):
        payload["截断"] = f"匹配 {matched}，按 max_entities={max_entities} 导出 {len(rows)}"
    if len(rows) > limit:
        payload["说明"] = f"已导出 {len(rows)} 行，仅回传前 {limit} 行，完整内容见 CSV/JSON"
    return json.dumps(payload, ensure_ascii=False, indent=2)


@mcp.tool()
def list_drawings() -> str:
    """列出项目各目录下的图纸文件。"""
    out: dict[str, list[str]] = {}
    for label, d in (("RAW", RAW), ("画图产物", DRAWN), ("图纸识别产物", RECOGNIZED)):
        if not d.exists():
            out[label] = []
            continue
        out[label] = sorted(
            f"{p.name}  ({p.stat().st_size // 1024} KB)"
            for p in d.iterdir()
            if p.suffix.lower() in (".dwg", ".dxf", ".json")
        )
    return json.dumps(out, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    mcp.run()
