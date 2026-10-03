"""天正识别的端到端自检：普查 → 记录 + 导出天正3 → 三方核对 → 识别。换机器、升级天正之后跑一遍。

    python tarch_selftest.py                 用仓库自带的样例图 fixtures/天正样例.dwg
    python tarch_selftest.py <某张天正图>      用你自己的图（只读，产物放在 %LOCALAPPDATA%\\acadmcp\\自检）

自带样例是在天正里用命令画出来的一小张图，识别结果有期望值（fixtures/天正样例_期望.json），逐项比。
自己的图没有期望值，以三方核对的结论为准。会拉起天正 AutoCAD，没提权时会自己提权。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import env
import tarch_recognize
import tarch_t3
import tarch_verify

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "天正样例.dwg"
EXPECTED = FIXTURE.with_name("天正样例_期望.json")
HOME = env.LOCAL / "自检" / "天正"


def observed(result: dict) -> dict:
    """识别结果里拿来跟期望值比的那几样：不带坐标，换机器、换字体都不该变。"""
    items = result["对象"]
    return {
        "按类": result["按类"],
        "文字": sorted(t for it in items for t in it.get("文字", [])),
        "尺寸显示值": sorted(d["显示"] for it in items for d in it.get("尺寸", [])),
        "墙": result["汇总"].get("墙", []),
        "门窗表": result["汇总"].get("门窗表", []),
    }


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    logging.getLogger("ezdxf").setLevel(logging.ERROR)
    ap = argparse.ArgumentParser(description="天正识别端到端自检")
    ap.add_argument("drawing", type=Path, nargs="?", help="要试的天正图，不给就用自带样例")
    ap.add_argument("--write-expected", action="store_true", help=argparse.SUPPRESS)
    a = ap.parse_args()
    own = a.drawing is None
    if own:
        # 样例先复制出去再转：转换会在图纸目录旁边建工作副本，不该动仓库里的文件
        raw = HOME / "样例"
        shutil.rmtree(HOME, ignore_errors=True)
        raw.mkdir(parents=True)
        shutil.copy2(FIXTURE, raw / FIXTURE.name)
        dwg = raw / FIXTURE.name
    else:
        dwg = a.drawing.resolve()
        if not dwg.exists():
            print(f"找不到图纸: {dwg}")
            return 1
        raw = dwg.parent
    out = HOME / f"产物-{hashlib.md5(str(raw).lower().encode('utf-8')).hexdigest()[:6]}"
    rel = dwg.name

    print(f"[1/3] 记录 + 导出天正3：{dwg}", flush=True)
    code = tarch_t3.convert(raw, out, files=[rel])
    census = json.loads((out / tarch_verify.CENSUS).read_text(encoding="utf-8")).get(rel, {})
    if not any(k.startswith("TCH_") for k in census.get("代理", {})):
        print(f"这张图没有天正对象（普查结果 {census}），自检不了")
        return 1
    if code or not tarch_verify.record_path(out, rel).exists():
        print(f"转换没成功，日志见 {out / '_work' / '_导出日志.txt'}")
        return 1

    print("[2/3] 三方核对（原图 / 记录 / 天正3）", flush=True)
    v = tarch_verify.verify(raw, out, rel)
    for key in ("对象齐全", "文字图元", "尺寸值", "拼回", "代理", "丢失文字", "乱码", "几何图元", "几何差异"):
        print(f"      {key}: {v[key]}")
    for problem in v["问题"]:
        print(f"      问题：{problem}")
    ok = v["结论"] == "通过"

    print("[3/3] 识别", flush=True)
    result = tarch_recognize.recognize_file(out, rel, v)
    print(f"      天正对象 {result['天正对象']} 个：{result['按类']}")
    print(f"      识别结果 {tarch_recognize.recognized_path(out, rel)}")

    if own:
        got = observed(result)
        if a.write_expected:
            EXPECTED.write_text(json.dumps(got, ensure_ascii=False, indent=1), encoding="utf-8")
            print(f"已写期望值 {EXPECTED}")
        want = json.loads(EXPECTED.read_text(encoding="utf-8"))
        for key in want:
            same = got.get(key) == want[key]
            ok = ok and same
            print(f"      期望值 {key}: {'一致' if same else '不一致'}" + ("" if same else f"\n        期望 {want[key]}\n        实得 {got.get(key)}"))
    print(f"\n{'通过' if ok else '不通过'}：天正图识别{'可用' if ok else '有问题，看上面的「问题」'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
