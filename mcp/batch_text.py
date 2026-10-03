"""批量导出全项目图纸文字。

    python batch_text.py <图纸目录> <产物目录> [--t3 天正3目录]

每张图一个 JSON，文件名是项目内相对路径（分隔符换成 __），内容是清洗后的
逐条文字加图层、类型。顶层文字之外的带「来源」：块属性 / 标注改写 / 多重引线 / 块内 / 代理图形
（见 acad.grab_text）；块内文字「引用次数」为 0 的在图面上看不到。图里有代理对象时 JSON 里记
「代理对象」：多少个、多少个带代理图形、多少个解码中断。

原图里的天正对象无头读不出文字，给了 --t3（tarch_t3.py 的产物目录）时，有 <名>_t3.dwg 的图改读天正3，
JSON 里记「读自」。天正3 里的天正文字是拆碎的，原文在 <名>_tch.json。
打不开的损坏图（ErrorStatus=190）用 acad.salvage 救回模型空间再读，「读自」记成修复副本。

统计里单列「超 250 字符的段落数」：MTEXT 正文超过 250 字符时 DXF 会拆成
组码 3 续段加组码 1 尾段，这些长段落正是设计说明、施工注意事项所在，
也是最容易被只读组码 1 的实现悄悄截断的部分（见 acad.entity_text）。

每张图独立进程，单张失败不影响整体；进度实时写盘，中断后重跑跳过已完成的。
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import traceback
import uuid
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import acad

TIMEOUT = 600
LONG = 250
DAMAGED = "ErrorStatus=190"


def safe_name(dwg: Path, raw: Path) -> str:
    rel = dwg.relative_to(raw).with_suffix("")
    return str(rel).replace("\\", "__").replace("/", "__") + ".json"


def read(dwg: Path, raw: Path, t3: Path | None) -> tuple[list[dict], str, dict[str, int]]:
    """返回 (文字, 读自, 代理图形计数)；读自为空表示读的是原图，图里没有代理对象时计数为空。"""
    src, origin = dwg, ""
    if t3:
        exported = (t3 / dwg.relative_to(raw)).with_name(dwg.stem + "_t3.dwg")
        if exported.exists():
            src, origin = exported, "天正3"
    res, items = acad.grab_text(src, timeout=TIMEOUT)
    if not res.ok and DAMAGED in res.log:
        fixed = acad.salvage(src, acad.WORK / ("salvaged-%s.dwg" % uuid.uuid4().hex[:12]))
        try:
            res, items = acad.grab_text(fixed, timeout=TIMEOUT)
        finally:
            fixed.unlink(missing_ok=True)
        origin = "修复副本"
    if not res.ok:
        raise RuntimeError("grab_text 返回 ok=False: %s" % res.error)
    return items, origin, res.stats


def main() -> int:
    ap = argparse.ArgumentParser(description="批量导出图纸文字")
    ap.add_argument("raw", type=Path, help="原始图纸目录")
    ap.add_argument("out", type=Path, help="产物目录")
    ap.add_argument("--t3", type=Path, help="tarch_t3.py 的产物目录，有天正3 的图改读天正3")
    a = ap.parse_args()
    # 有的原图句柄重复，ezdxf 每读一次刷一屏 WARNING（解代理图形时会读 DXF）
    logging.getLogger("ezdxf").setLevel(logging.ERROR)
    raw, out = a.raw.resolve(), a.out.resolve()
    t3 = a.t3.resolve() if a.t3 else None
    if not raw.exists():
        print("找不到图纸目录: %s" % raw)
        return 1
    out.mkdir(parents=True, exist_ok=True)
    prog_file = out / "_progress.json"
    prog = json.loads(prog_file.read_text(encoding="utf-8")) if prog_file.exists() else {}

    dwgs = sorted(raw.rglob("*.dwg"))
    print("图纸总数: %d" % len(dwgs), flush=True)
    t0 = time.time()
    done = skipped = failed = 0
    items_sum = long_sum = 0
    source_sum: Counter[str] = Counter()

    for i, dwg in enumerate(dwgs, 1):
        key = str(dwg)
        if prog.get(key, {}).get("done"):
            skipped += 1
            items_sum += prog[key].get("条数", 0)
            long_sum += prog[key].get("超250字符段落", 0)
            source_sum.update(prog[key].get("来源", {}))
            continue
        t1 = time.time()
        try:
            items, origin, proxy_stats = read(dwg, raw, t3)
            longs = {it["文字"] for it in items if len(it["文字"]) > LONG}
            sources = Counter(
                "块内未引用" if it.get("引用次数") == 0 else it.get("来源", "顶层") for it in items
            )
            record = {"图纸": key, "条数": len(items), "来源": dict(sources), "超250字符段落": len(longs)}
            if origin:
                record["读自"] = origin
            if proxy_stats:
                record["代理对象"] = proxy_stats
            (out / safe_name(dwg, raw)).write_text(
                json.dumps({**record, "文字": items}, ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
            prog[key] = {"done": True, **{k: v for k, v in record.items() if k != "图纸"},
                         "耗时": round(time.time() - t1, 1)}
            items_sum += len(items)
            long_sum += len(longs)
            source_sum.update(sources)
            done += 1
            print(
                "[%d/%d] %.1fs %d 条 %s 长段落%d %s %s"
                % (i, len(dwgs), time.time() - t1, len(items), dict(sources), len(longs),
                   origin and "读自" + origin, dwg.relative_to(raw)),
                flush=True,
            )
        except Exception as exc:
            failed += 1
            prog[key] = {"done": False, "错误": "%s: %s" % (type(exc).__name__, exc)}
            print("[%d/%d] 失败 %s -> %s" % (i, len(dwgs), dwg.relative_to(raw), exc),
                  flush=True)
            traceback.print_exc()
        prog_file.write_text(json.dumps(prog, ensure_ascii=False, indent=1), encoding="utf-8")

    print(flush=True)
    print("=" * 50, flush=True)
    print("完成 %d | 跳过(缓存) %d | 失败 %d | 总耗时 %.1f 分钟"
          % (done, skipped, failed, (time.time() - t0) / 60), flush=True)
    print("文字总条数 %d | 超 %d 字符的长段落 %d 条" % (items_sum, LONG, long_sum),
          flush=True)
    print("按来源: %s" % dict(source_sum), flush=True)
    print("输出目录: %s" % out, flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
