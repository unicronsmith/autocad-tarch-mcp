"""重画自检用的天正样例图：在天正会话里敲命令，画墙、文字、尺寸、坐标、标高、门窗、柱、引出标注，另存。

    python make_fixture.py [输出.dwg]      要在管理员终端里跑（天正的 AutoCAD 总是提权运行）

画完用 `python ../tarch_selftest.py --write-expected` 重写期望值。平时用不着：仓库里已经带了画好的
天正样例.dwg，只有天正版本变了、想让样例图跟着变时才重画。
"""
from __future__ import annotations

import ctypes
import sys
import time
from collections import Counter
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import win32con
import acad_com
import tarch_t3

ESC = "\x1b\x1b"
# (送进命令行的字, 之后等几秒)。天正的绘图命令一上来弹非模态的参数框，默认参数直接可用，点位照样从命令行收
STEPS = [
    ("TGWALL\n", 4), ("0,0\n", 2), ("6000,0\n", 2), ("6000,4000\n", 2), ("0,4000\n", 2), ("0,0\n", 2), ("\n", 2), ("\n", 2), (ESC, 2),
    ("TTEXT\n", 4), ("1000,1000\n", 2), ("\n", 2), (ESC, 2),
    ("TDIMMP\n", 3), ("0,0\n", 2), ("6000,0\n", 2), ("3000,-1200\n", 2), ("\n", 2), ("\n", 2), (ESC, 2),
    ("TCOORD\n", 3), ("3000,2000\n", 2), ("4500,3000\n", 2), ("\n", 2), (ESC, 2),
    ("TMELEV\n", 4), ("2000,3000\n", 2), ("2500,3500\n", 2), ("\n", 2), (ESC, 2),
    ("TOPENING\n", 4), ("3000,0\n", 2), ("\n", 2), (ESC, 2),
    ("TGCOLUMN\n", 4), ("6000,4000\n", 2), ("\n", 2), (ESC, 2),
    ("TLEADER\n", 4), ("4000,1000\n", 2), ("5000,1800\n", 2), ("5600,1800\n", 2), ("\n", 2), (ESC, 2),
]


class COPYDATASTRUCT(ctypes.Structure):
    _fields_ = [("dwData", ctypes.c_void_p), ("cbData", wintypes.DWORD), ("lpData", ctypes.c_void_p)]


def type_in(hwnd: int, text: str) -> None:
    # 命令执行到一半时 COM 的 SendCommand 会被拒（RPC_E_SERVERCALL_RETRYLATER，等多久都一样）；
    # AutoCAD 主窗口收 WM_COPYDATA（dwData=1，UTF-16 字符串）当作命令行输入，命令中途也收
    buf = ctypes.create_unicode_buffer(text)
    cds = COPYDATASTRUCT(1, ctypes.sizeof(buf), ctypes.cast(buf, ctypes.c_void_p))
    ctypes.windll.user32.SendMessageTimeoutW(hwnd, win32con.WM_COPYDATA, 0, ctypes.byref(cds), 0, 5000, None)


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    if not ctypes.windll.shell32.IsUserAnAdmin():
        print("要在管理员终端里跑：天正的 AutoCAD 总是提权运行，没提权的进程连不上它")
        return 1
    dst = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).with_name("天正样例.dwg")
    profile = tarch_t3.remember_profile()
    app, pid = tarch_t3.launch()
    try:
        docs = acad_com.live(lambda: app.Documents, "Count")
        doc = acad_com.retry(lambda: docs.Add())
        tarch_t3.quiet(app)
        for text, wait in STEPS:
            type_in(app.HWND, text)
            time.sleep(wait)
        type_in(app.HWND, ESC)
        tarch_t3.quiet(app, 20)
        objs = tarch_t3.record(doc, pid, set())
        print("天正对象", dict(Counter(o["dxf"] for o in objs)), flush=True)
        if not objs:
            print("一个天正对象都没画出来，没有另存")
            return 1
        dst.unlink(missing_ok=True)
        acad_com.retry(lambda: doc.SaveAs(str(dst)))
        acad_com.retry(lambda: doc.Close(False), tries=40)
        print("已另存", dst, flush=True)
        return 0
    finally:
        if tarch_t3.alive(app):
            tarch_t3.quit_own(app, pid)
        if pid in tarch_t3.acad_pids():
            tarch_t3.kill_own(pid)
        if tarch_t3.current_profile() != profile:
            tarch_t3.set_profile(profile)


if __name__ == "__main__":
    raise SystemExit(main())
