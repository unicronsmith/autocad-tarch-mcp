"""天正图转天正3：先无头普查哪些图含天正对象，再用正式版天正 T30 逐张记录、导出。

天正对象只有天正主程序能还原：免费 T30 插件只有类定义，分解、导出都做不到；tch_kernal.arx
要从 ACAD.exe 导入符号，accoreconsole 加载不了。所以只能在 TGStart.exe 拉起的 acad.exe
里做。那个 acad.exe 带 RUNASADMIN 兼容标记、总是提权运行，非提权进程连不上它的 COM ——
本脚本发现自己没提权，就以管理员身份重跑自己，把输出转回当前控制台。

每张图在天正会话里做两件事：

1. 记录：加载 tchdump 插件（tchdump/TchDump.cs），在进程内把每个天正对象分解一遍、读一遍属性，
   写进 <名>_tch.json —— 每个对象画出来的图元（文字、尺寸值、线、块参照）和语义属性（墙高、门窗编号、
   管径）都按句柄对得上。天正3 里这些全散了：文字拆成碎片、属性丢了、分不清哪条线是哪个对象的。
2. 导出：TSAVEAS 导成天正3（<名>_t3.dwg），没装天正的环境也能读。

    python tarch_t3.py <RAW目录> <产物目录> [--only 子串,子串] [--rerecord]

产物目录镜像 RAW 的目录结构，每张图出 <名>_t3.dwg + <名>_tch.json；根上是 _天正普查.json
和 _导出汇总.jsonl；_work 放工作副本，RAW 只读不碰。已有产物的图跳过，断点续跑。
只结束、退出本脚本自己拉起的 AutoCAD，用户开着的实例不碰。
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import traceback
import uuid
import winreg
from collections import Counter
from pathlib import Path

import pythoncom
import pywintypes
import win32con
import win32event
import win32gui
import win32process
import win32com.client
from pywinauto import Application
from win32com.shell import shell, shellcon

sys.path.insert(0, str(Path(__file__).resolve().parent))
import acad
import acad_com as C
import env
from tarch_verify import CENSUS, matches, record_path, t3_path, tarch_drawings

TGSTART = env.TARCH.tgstart
TARCH_PROFILE = env.TARCH.profile
ACAD_PROGID = env.ACAD.progid
ACAD_REG = env.ACAD.reg
PLUGIN = Path(__file__).resolve().parent / "tchdump" / f"R{env.ACAD.major}" / "TchDump.dll"
RECORD_VERSION = 2
EXPORT_LOG = "_导出汇总.jsonl"
# 单张导出的上限；2026-10-01 实测 41 张里最慢一张 107s
LIMIT = 1800
# 插件分解加读属性的上限。2026-10-03 实测 1812 个对象的图 2.1s；到这个时间还没写完就是 AutoCAD 崩了或卡死
PLUGIN_LIMIT = 180
# 进程已崩：RPC 服务器不可用 / 对象已断开 / 远程过程调用失败。忙（acad_com.BUSY_HRESULTS）不算
DEAD_HRESULTS = (-2147023174, -2147417848, -2147023170)
T3_TYPE = "天正3文件"
CAD_VERSION = "AutoCAD 2018文件"
# 插件读某个属性、分解某类对象时 AutoCAD 当场崩了，就从它输出的最后一行跟踪认出肇事项记进这里，以后跳过；
# 跟天正版本走，不跟项目走。格式见 TchDump.cs
DENY_FILE = env.LOCAL / "tarch_deny.json"
PROFILE_FILE = env.LOCAL / "profile_before_tarch.txt"
# 同一张图因为插件崩溃而重试的上限：每次只能认出一个肇事项
RETRY = 5


class Stuck(RuntimeError):
    """插件没跑完：AutoCAD 崩了或卡死。culprit 是从跟踪行认出来的 (类名, 属性名 / * / !explode)，认不出是 None。"""

    def __init__(self, msg: str, culprit: tuple[str, str] | None = None):
        super().__init__(msg)
        self.culprit = culprit


def proxy_classes(dxf: Path) -> Counter:
    """代理对象按原类名计数。代理对象的组码 91 是类 ID：CLASSES 段第一个类是 500，往后顺延。"""
    classes: list[str] = []
    found: Counter = Counter()
    section = ""
    want = ""
    with dxf.open(encoding="utf-8", errors="replace") as f:
        for code, value in zip(f, f):
            code, value = code.strip(), value.strip()
            if code == "0":
                want = ("section" if value == "SECTION" else "class" if section == "CLASSES" and value == "CLASS"
                        else "proxy" if value == "ACAD_PROXY_ENTITY" else "")
            elif code == "2" and want == "section":
                section, want = value, ""
            elif code == "1" and want == "class":
                classes.append(value)
                want = ""
            elif code == "91" and want == "proxy":
                i = int(value) - 500
                found[classes[i] if 0 <= i < len(classes) else f"类ID{value}"] += 1
                want = ""
    return found


def census_one(dwg: Path) -> dict:
    dxf = acad.WORK / f"census-{uuid.uuid4().hex[:12]}.dxf"
    try:
        return {"代理": dict(proxy_classes(acad.dxfout(dwg, dxf)).most_common())}
    except acad.AcadError as ex:
        return {"错误": str(ex)[:300]}
    finally:
        dxf.unlink(missing_ok=True)


def census(raw: Path, out: Path, files: list[str] | None = None) -> dict[str, dict]:
    """逐张无头 DXFOUT，数各图的代理对象类。逐张落盘，普查过的跳过，出错的下次重跑。给了 files 就只查这几张。"""
    path = out / CENSUS
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    dwgs = [raw / f for f in files] if files else sorted(raw.rglob("*.dwg"))
    for n, dwg in enumerate(dwgs, 1):
        rel = str(dwg.relative_to(raw))
        if "代理" in data.get(rel, {}):
            continue
        t0 = time.time()
        data[rel] = census_one(dwg)
        print(f"[普查 {n}/{len(dwgs)}] {time.time() - t0:.1f}s 代理 {sum(data[rel].get('代理', {}).values())} 个"
              f"{' 出错' if '错误' in data[rel] else ''}  {rel}", flush=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return data


def process_ids(image: str) -> set[int]:
    out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {image}", "/FO", "CSV", "/NH"],
                         capture_output=True, text=True, encoding="mbcs").stdout
    return {int(ln.split('","')[1]) for ln in out.splitlines() if ln.lower().startswith(f'"{image.lower()}"')}


def acad_pids() -> set[int]:
    return process_ids("acad.exe")


def kill_own(pid: int) -> None:
    """强制结束本脚本拉起的 AutoCAD，连同它身后冒出来的「AutoCAD 错误报告」窗（cer_dialog.exe）。

    错误报告窗会留在用户屏幕上；2026-10-03 还实测到一次它开着的时候下一个天正实例起不来
    （TGStart 之后新进程 180s 没进运行对象表，关掉它重试就正常）。只结束强杀之后新冒出来的那几个。
    """
    before = process_ids("cer_dialog.exe")
    subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
    t0 = time.time()
    while time.time() - t0 < 20:
        for new in process_ids("cer_dialog.exe") - before:
            subprocess.run(["taskkill", "/F", "/PID", str(new)], capture_output=True)
        time.sleep(1)


def connect_pid(pid: int, timeout: float = 180):
    """按进程号从运行对象表里找 AutoCAD，免得连到用户自己开的实例上。"""
    clsid = str(pywintypes.IID(ACAD_PROGID)).strip("{}")
    t0 = time.time()
    while time.time() - t0 < timeout:
        rot = pythoncom.GetRunningObjectTable()
        for mk in rot:
            try:
                if clsid not in mk.GetDisplayName(pythoncom.CreateBindCtx(0), None).upper():
                    continue
                app = win32com.client.Dispatch(rot.GetObject(mk).QueryInterface(pythoncom.IID_IDispatch))
                if win32process.GetWindowThreadProcessId(app.HWND)[1] == pid:
                    return app
            except Exception:
                continue
        time.sleep(2)
    raise RuntimeError(f"{timeout}s 内在运行对象表里没找到 PID {pid} 的 AutoCAD")


def quiet(app, limit: float = 60.0) -> bool:
    t0 = time.time()
    while time.time() - t0 < limit:
        try:
            if app.GetAcadState().IsQuiescent:
                return True
        except Exception:
            pass
        time.sleep(0.3)
    return False


def launch(kill: int | None = None):
    """经 TGStart 拉起一个新的天正 AutoCAD，返回 (app, pid)。kill 是本脚本上一个已经挂掉的实例。"""
    if kill and kill in acad_pids():
        kill_own(kill)
    before = acad_pids()
    subprocess.Popen([str(TGSTART)], cwd=str(TGSTART.parent))
    t0 = time.time()
    while time.time() - t0 < 120:
        new = acad_pids() - before
        if new:
            pid = new.pop()
            app = connect_pid(pid)
            quiet(app, 120)
            return app, pid
        time.sleep(2)
    raise RuntimeError(f"TGStart 之后 120s 内没有新的 acad.exe（已在运行的: {sorted(before)}）")


def alive(app) -> bool:
    """持续忙也算挂了：弹出“AutoCAD 错误中断”致命错误框时，每个调用都回“正在使用中”。"""
    try:
        C.retry(lambda: app.Version, tries=480)
        return True
    except (C.ComError, AttributeError):
        # 进程已经没了的时候，win32com 的动态分发抛的是 AttributeError 而不是 com_error
        return False
    except pywintypes.com_error as ex:
        if ex.hresult in DEAD_HRESULTS:
            return False
        raise


def current_profile() -> str:
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, rf"{ACAD_REG}\Profiles") as k:
        return winreg.QueryValueEx(k, "")[0]


def set_profile(name: str) -> None:
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, rf"{ACAD_REG}\Profiles", 0, winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, "", 0, winreg.REG_SZ, name)


def remember_profile() -> str:
    """开工前 AutoCAD 的当前配置，收尾时要改回它。

    天正启动器把当前配置切到天正；天正正常退出时改回「启动那一刻的配置」，崩了或被强制结束就留在天正上。
    上一轮要是没收好尾，这次读到的已经是天正配置，天正退出也只会「改回」天正（2026-10-03 实测）。
    所以每次把不是天正的那个配置名记到文件里，读到天正配置时照文件里的改回去。
    """
    now = current_profile()
    if now != TARCH_PROFILE:
        PROFILE_FILE.parent.mkdir(parents=True, exist_ok=True)
        PROFILE_FILE.write_text(now, encoding="utf-8")
        return now
    return PROFILE_FILE.read_text(encoding="utf-8") if PROFILE_FILE.exists() else now


def quit_own(app, pid: int) -> None:
    """有未保存的图纸就不退出：Quit 会弹保存确认框，把这边一起挂住。"""
    try:
        docs = C.live(lambda: app.Documents, "Count")
        if any(not C.retry(lambda i=i: docs.Item(i).Saved) for i in range(C.retry(lambda: docs.Count))):
            print(f"天正 AutoCAD（PID {pid}）里有未保存的图纸，不退出", flush=True)
            return
        C.retry(lambda: app.Quit())
    except Exception as ex:
        print(f"退出天正 AutoCAD（PID {pid}）出错 {type(ex).__name__}: {ex}", flush=True)
        return
    t0 = time.time()
    while pid in acad_pids() and time.time() - t0 < 60:
        time.sleep(1)
    print(f"天正 AutoCAD（PID {pid}）已退出={pid not in acad_pids()}", flush=True)


def load_deny() -> set[tuple[str, str]]:
    return {tuple(x) for x in json.loads(DENY_FILE.read_text(encoding="utf-8"))} if DENY_FILE.exists() else set()


def read_jsonl(path: Path) -> tuple[list[dict], bool, str]:
    """插件的输出：返回 (对象, 写没写完, 最后一行跟踪)。「#」开头的是跟踪行，末尾有 {"done": n} 才算写完。"""
    objs, done, last = [], False, ""
    if not path.exists():
        return objs, done, last
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("#"):
            last = line[1:]
        elif line.startswith("{"):
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "done" in o:
                done = True
            else:
                objs.append(o)
    return objs, done, last


def culprit_of(dump_last: str, dump_done: bool, props_last: str) -> tuple[str, str] | None:
    """从没写完的输出认肇事项。分解的跟踪行是「句柄 DXF名」；读属性的是「类名<TAB>属性名」或「句柄 类名」。"""
    if not dump_done:
        cols = dump_last.split(" ")
        return (cols[1], "!explode") if len(cols) == 2 else None
    if "\t" in props_last:
        cls, prop = props_last.split("\t", 1)
        return cls, prop
    cols = props_last.split(" ")
    return (cols[1], "*") if len(cols) == 2 else None


def record(doc, pid: int, deny: set[tuple[str, str]]) -> list[dict]:
    """在天正会话里跑插件，返回每个天正对象的记录：分解出的图元（parts）加属性（props）。"""
    if not PLUGIN.exists():
        raise RuntimeError(f"没有给 AutoCAD R{env.ACAD.major} 编好的插件 {PLUGIN}，编法见 tchdump/TchDump.csproj")
    acad.WORK.mkdir(parents=True, exist_ok=True)
    stamp = uuid.uuid4().hex[:12]
    dump, props, deny_file, done = (acad.WORK / f"tch-{stamp}.{ext}" for ext in ("dump", "props", "deny", "done"))
    deny_file.write_text("\n".join(f"{a}\t{b}" for a, b in sorted(deny)), encoding="utf-8")
    q = lambda p: acad.lisp_str(acad.lisp_path(p))
    lines = [f'(command "_.NETLOAD" {q(PLUGIN)})', f"(tchdump {q(dump)} {q(deny_file)})",
             f"(tchprops {q(props)} {q(deny_file)})",
             f'(progn (setq _d (open {q(done)} "w")) (write-line "ok" _d) (close _d) (princ))']
    # SECURELOAD 默认为 1：不在受信任目录里的 DLL，NETLOAD 会弹“未签名的可执行文件”框。临时把插件目录加进去，用完改回
    trusted = C.retry(lambda: doc.GetVariable("TRUSTEDPATHS"))
    listed = any(Path(p.rstrip("\\.")) == PLUGIN.parent for p in trusted.split(";") if p.strip())
    if not listed:
        C.retry(lambda: doc.SetVariable("TRUSTEDPATHS", f"{trusted};{PLUGIN.parent}" if trusted else str(PLUGIN.parent)))
    try:
        for ln in lines:
            C.retry(lambda ln=ln: doc.SendCommand(ln + "\n"))
        t0 = time.time()
        while not done.exists():
            if time.time() - t0 > PLUGIN_LIMIT or pid not in acad_pids():
                _, dump_done, dump_last = read_jsonl(dump)
                _, _, props_last = read_jsonl(props)
                raise Stuck(f"插件没跑完，停在 {(props_last if dump_done else dump_last) or '还没开始'}",
                            culprit_of(dump_last, dump_done, props_last))
            time.sleep(0.2)
        objs, dump_done, _ = read_jsonl(dump)
        prop_objs, props_done, _ = read_jsonl(props)
        if not (dump_done and props_done):
            raise RuntimeError("插件输出不完整：NETLOAD 没成功，或 tchdump / tchprops 中途出错")
        by_handle = {o["h"]: o for o in prop_objs}
        for o in objs:
            p = by_handle.get(o["h"], {})
            o["props"] = p.get("props", {})
            for key in ("failed", "err"):
                if key in p:
                    o["props_" + key] = p[key]
        return objs
    finally:
        if not listed:
            try:
                C.retry(lambda: doc.SetVariable("TRUSTEDPATHS", trusted), tries=40)
            except Exception:
                pass
        for f in (dump, props, deny_file, done):
            f.unlink(missing_ok=True)


def windows_of(pid: int) -> set:
    found = []

    def cb(h, _):
        if win32gui.IsWindowVisible(h) and win32process.GetWindowThreadProcessId(h)[1] == pid:
            found.append(h)
    win32gui.EnumWindows(cb, None)
    return set(found)


def send_async(doc, text: str) -> threading.Event:
    """SendCommand 要等命令跑完才返回，而 TSAVEAS 先弹模态框，所以换个线程发，这边去填框。"""
    stream = pythoncom.CoMarshalInterThreadInterfaceInStream(pythoncom.IID_IDispatch, doc._oleobj_)
    done = threading.Event()

    def run():
        pythoncom.CoInitialize()
        try:
            d = win32com.client.Dispatch(pythoncom.CoGetInterfaceAndReleaseStream(stream, pythoncom.IID_IDispatch))
            d.SendCommand(text)
        except Exception:
            pass
        finally:
            done.set()
    threading.Thread(target=run, daemon=True).start()
    return done


def pick(combo, prefix: str) -> str:
    for item in combo.texts()[1:]:
        if item.strip().startswith(prefix):
            combo.select(item)
            return item
    raise RuntimeError(f"下拉框里没有以 {prefix!r} 开头的选项: {combo.texts()[1:]}")


def fill_export_dialog(pw, h, dst: Path, log: list, step) -> None:
    dlg = pw.window(handle=h)
    step("枚举控件")
    combos = {}
    for c in dlg.descendants():
        if c.friendly_class_name() != "ComboBox":
            continue
        items = c.texts()[1:]
        if any(T3_TYPE in i for i in items):
            combos["type"] = c
        elif any(i.startswith("AutoCAD") for i in items):
            combos["cad"] = c
        elif "全部内容" in items:
            combos["content"] = c
    step("选保存类型")
    log.append("保存类型=" + pick(combos["type"], T3_TYPE))
    step("选CAD版本")
    log.append("CAD版本=" + pick(combos["cad"], CAD_VERSION))
    step("选导出内容")
    log.append("导出内容=" + pick(combos["content"], "全部内容"))
    step("勾选项")
    for c in dlg.descendants():
        name, cls = c.window_text(), c.friendly_class_name()
        if cls == "CheckBox" and name in ("绑定参照", "图块名称保持不变"):
            c.check()
        elif cls == "CheckBox" and name == "视口转块":
            c.uncheck()
        elif cls == "RadioButton" and name == "绑定":
            c.check()
    step("填文件名")
    next(c for c in dlg.descendants() if c.friendly_class_name() == "Edit").set_edit_text(str(dst))
    step("点保存")
    # 投递而不是同步发送：导出在保存按钮的处理函数里同步执行，同步点击会把这边一起挂住
    save = dlg.child_window(title="保存(&S)", class_name="Button")
    win32gui.PostMessage(h, win32con.WM_COMMAND, save.control_id(), save.handle)
    step("已投递保存")


def answer_popups(pw, pid: int, known: set, log: list, stop: threading.Event) -> None:
    """导出途中冒出的其它对话框：记下原文后按默认按钮。"""
    while not stop.is_set():
        for h in windows_of(pid) - known:
            try:
                w = pw.window(handle=h)
                if w.class_name() == "#32770":
                    texts = [c.window_text() for c in w.descendants() if c.window_text()]
                    log.append(f"弹框[{w.window_text()}]: {texts[:8]}")
                    btn = next((c for c in w.descendants() if c.friendly_class_name() == "Button"
                                and c.window_text().replace("&", "") in ("是(Y)", "确定", "是", "OK", "Yes(Y)")), None)
                    btn.click() if btn else w.type_keys("{ENTER}")
            except Exception as ex:
                log.append(f"弹框处理异常 {ex}")
            known.add(h)
        time.sleep(0.3)


def tarch_font_dirs() -> list[Path]:
    """天正会话找字体的目录：TGStart 用 /p <天正配置> 拉起 AutoCAD，搜索路径在这个配置的注册表里。"""
    key = rf"{ACAD_REG}\Profiles\{TARCH_PROFILE}\General"
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
        paths = winreg.QueryValueEx(k, "ACAD")[0]
    return [Path(os.path.expandvars(p)) for p in paths.split(";") if p.strip()] + [acad.ACAD_DIR / "Fonts"]


def mirror(raw: Path, work: Path, rel: str, font_dirs: list[Path]) -> Path:
    """整个目录的 DWG 一起复制：外部参照按相对路径找，得和主图放在一起。

    天正会话找不到的大字体补到副本旁边（AutoCAD 先在图纸目录找字体）：它的搜索路径里有 hztxt.shx，
    缺的大字体会被它顶替，读图时中文就解成乱码，再原样写进天正3（见 acad.FONTS）。
    找得到的真字体不顶替，免得字宽变了、天正对象的包围盒跟着变。
    """
    src = raw / rel
    dst_dir = work / Path(rel).parent
    dst_dir.mkdir(parents=True, exist_ok=True)
    for f in src.parent.glob("*.dwg"):
        t = dst_dir / f.name
        if not t.exists() or t.stat().st_size != f.stat().st_size:
            shutil.copy2(f, t)
    missing = [n for n in acad.bigfonts(src) if not any((d / n).exists() for d in font_dirs)]
    acad.alias_bigfonts(missing, dst_dir)
    return dst_dir / src.name


def tsaveas(app, doc, pid: int, dst: Path, res: dict) -> None:
    pw = Application(backend="win32").connect(process=pid)
    before = windows_of(pid)
    dst.unlink(missing_ok=True)
    done = send_async(doc, "_TSAVEAS\n")
    h = None
    for _ in range(240):
        time.sleep(0.25)
        cand = [x for x in windows_of(pid) - before if win32gui.GetWindowText(x) == "图形导出"]
        if cand:
            h = cand[0]
            break
    if not h:
        raise RuntimeError("60s 内没出现「图形导出」对话框")
    log, steps = [], []
    filler = threading.Thread(target=fill_export_dialog, args=(pw, h, dst, log, steps.append), daemon=True)
    filler.start()
    filler.join(60)
    if filler.is_alive() or "已投递保存" not in steps:
        win32gui.PostMessage(h, win32con.WM_COMMAND, win32con.IDCANCEL, 0)
        raise RuntimeError(f"填「图形导出」{'超过 60s' if filler.is_alive() else '中途出错'}，"
                           f"停在：{steps[-1] if steps else '未开始'}，已取消对话框")
    stop = threading.Event()
    threading.Thread(target=answer_popups, args=(pw, pid, before | {h}, log, stop), daemon=True).start()
    done.wait(LIMIT)
    quiet(app, LIMIT)
    stop.set()
    res["弹框"] = [x for x in log if x.startswith("弹框")]
    res["提示"] = C.retry(lambda: doc.GetVariable("LASTPROMPT"))


def export_one(app, pid: int, work: Path, out: Path, rel: str, deny: set[tuple[str, str]],
               record_only: bool = False) -> dict:
    dst = t3_path(out, rel)
    dst.parent.mkdir(parents=True, exist_ok=True)
    res = {"图纸": rel}
    t0 = time.time()
    docs = C.live(lambda: app.Documents, "Count")
    C.retry(lambda: docs.Open(str(work), True))

    def target():
        for i in range(docs.Count):
            d = docs.Item(i)
            if d.FullName.lower() == str(work).lower():
                return d
        raise AttributeError("工作副本还没出现在 Documents 集合")

    doc = C.live(target, "FullName")
    quiet(app)
    res["打开s"] = round(time.time() - t0, 1)
    try:
        objs = record(doc, pid, deny)
        record_path(out, rel).write_text(
            json.dumps({"版本": RECORD_VERSION, "图纸": rel, "天正": env.TARCH.version, "对象": objs}, ensure_ascii=False),
            encoding="utf-8")
        res["天正对象"] = len(objs)
        res["类"] = dict(Counter(o["dxf"] for o in objs).most_common(8))
        res["分解不了"] = sum(1 for o in objs for p in o["parts"] if p["type"] == "!") + sum(1 for o in objs if "err" in o)
        res["没读属性"] = sum(1 for o in objs if not o["props"])
        res["记录s"] = round(time.time() - t0, 1)
        if record_only:
            res["仅记录"] = True
        else:
            tsaveas(app, doc, pid, dst, res)
            res["导出s"] = round(time.time() - t0, 1)
        res["产物"] = dst.exists()
    finally:
        try:
            C.retry(lambda: doc.Close(False), tries=40)
        except Exception as ex:
            res["关闭出错"] = str(ex)[:80]
    return res


def watchdog(finished: threading.Event, pid: int, rel: str) -> None:
    if not finished.wait(LIMIT + 120):
        print(f"{rel} 超时，强制结束本脚本拉起的 AutoCAD（PID {pid}）", flush=True)
        kill_own(pid)


def log_result(out: Path, res: dict) -> None:
    print(json.dumps(res, ensure_ascii=False), flush=True)
    with (out / EXPORT_LOG).open("a", encoding="utf-8") as f:
        f.write(json.dumps(res, ensure_ascii=False) + "\n")


def export_all(raw: Path, out: Path, pending: list[str], record_only: bool = False) -> int:
    # 无头步骤（mirror 里读文字样式）要在拉起天正之前做完：天正运行期间 accoreconsole 读图没跑完 LISP 就退出
    # （2026-10-01 实测两张图都这样，天正退出后同一调用正常）
    works, failed = {}, 0
    font_dirs = tarch_font_dirs()
    for rel in pending:
        try:
            works[rel] = mirror(raw, out / "_work", rel, font_dirs)
        except acad.AcadError as ex:
            failed += 1
            log_result(out, {"图纸": rel, "错误": f"准备工作副本失败: {str(ex)[:300]}"})
    if not works:
        return 1
    deny = load_deny()
    verb = "记录" if record_only else "导出"
    profile = remember_profile()
    print(f"待{verb} {len(works)} 张，每张图单独开一个天正会话，黑名单 {len(deny)} 项", flush=True)
    pid = None
    try:
        for n, (rel, work) in enumerate(works.items(), 1):
            found = []
            for attempt in range(RETRY):
                # 一张图一个会话：2026-10-01/02 同一会话里连着处理两三张图，再开下一张就访问冲突崩掉。
                # 那时属性是 Python 跨进程读的；改成插件进程内读之后没重测过连开，先照旧
                app, pid = launch(kill=pid)
                print(f"[{verb} {n}/{len(works)}] {rel}（天正 PID {pid}）" + (f"第 {attempt + 1} 次" if attempt else ""),
                      flush=True)
                finished = threading.Event()
                threading.Thread(target=watchdog, args=(finished, pid, rel), daemon=True).start()
                culprit = None
                try:
                    res = export_one(app, pid, work, out, rel, deny, record_only)
                except Stuck as ex:
                    culprit = ex.culprit
                    res = {"图纸": rel, "错误": f"Stuck: {ex}"}
                except Exception as ex:
                    res = {"图纸": rel, "错误": f"{type(ex).__name__}: {str(ex)[:300]}", "堆栈": traceback.format_exc()[-600:]}
                finally:
                    finished.set()
                if alive(app):
                    quit_own(app, pid)
                    if not culprit:
                        break
                else:
                    print(f"AutoCAD（PID {pid}）崩了或卡死", flush=True)
                if not culprit or culprit in deny:
                    break
                deny.add(culprit)
                found.append("\t".join(culprit))
                DENY_FILE.parent.mkdir(parents=True, exist_ok=True)
                DENY_FILE.write_text(json.dumps(sorted(deny), ensure_ascii=False), encoding="utf-8")
                print(f"插件停在 {culprit[0]} / {culprit[1]}，记进 {DENY_FILE} 后重试这张图", flush=True)
            if found:
                res["新进黑名单"] = found
            failed += "错误" in res or not (res.get("产物") or record_only)
            log_result(out, res)
    finally:
        if pid and pid in acad_pids():
            kill_own(pid)
        # 天正正常退出会把当前配置改回去，崩了或被强制结束就停在天正配置，用户下次开 AutoCAD 会进天正
        if current_profile() != profile:
            set_profile(profile)
            print(f"当前配置已改回 {profile}", flush=True)
    print(f"{verb}结束：{len(pending)} 张，失败 {failed} 张", flush=True)
    return 1 if failed else 0


def rerun_elevated(argv: list[str], log: Path) -> int:
    """以管理员身份重跑自己：子进程把输出写进 log，这边边读边打印，返回子进程的退出码。"""
    log.parent.mkdir(parents=True, exist_ok=True)
    log.unlink(missing_ok=True)
    params = subprocess.list2cmdline([str(Path(__file__).resolve()), *argv, "--log", str(log)])
    info = shell.ShellExecuteEx(fMask=shellcon.SEE_MASK_NOCLOSEPROCESS, lpVerb="runas", lpFile=sys.executable,
                                lpParameters=params, lpDirectory=str(Path(__file__).resolve().parent),
                                nShow=win32con.SW_HIDE)
    proc, pos = info["hProcess"], 0
    print(f"已以管理员身份重跑，日志 {log}", flush=True)
    while True:
        exited = win32event.WaitForSingleObject(proc, 1000) == win32event.WAIT_OBJECT_0
        if log.exists():
            with log.open("rb") as f:
                f.seek(pos)
                chunk = f.read()
            cut = len(chunk) if exited else chunk.rfind(b"\n") + 1
            if cut:
                sys.stdout.write(chunk[:cut].decode("utf-8", errors="replace"))
                sys.stdout.flush()
                pos += cut
        if exited:
            return win32process.GetExitCodeProcess(proc)


def convert(raw: Path, out: Path, only: str = "", rerecord: bool = False, files: list[str] | None = None) -> int:
    """普查 → 记录 + 导出。没提权时以管理员身份重跑自己。返回 0 表示全部成功。
    files 是相对 raw 的路径，给了就只处理这几张，不扫整个目录。"""
    out.mkdir(parents=True, exist_ok=True)
    data = census(raw, out, files)
    bad = [rel for rel, r in data.items() if "错误" in r and (not files or rel in files)]
    if bad:
        print(f"普查出错 {len(bad)} 张，不知道含不含天正对象，下次重跑会再试: {bad}", flush=True)
    todo = [rel for rel in tarch_drawings(data) if matches(rel, only) and (not files or rel in files)]
    if rerecord:
        pending = todo
        print(f"含天正对象 {len(todo)} 张，重写记录 {len(pending)} 张", flush=True)
    else:
        pending = [rel for rel in todo if not (t3_path(out, rel).exists() and record_path(out, rel).exists())]
        print(f"含天正对象 {len(todo)} 张，已导出 {len(todo) - len(pending)} 张，待导出 {len(pending)} 张", flush=True)
    if not pending:
        return 0
    if not ctypes.windll.shell32.IsUserAnAdmin():
        argv = [str(raw), str(out)] + (["--only", only] if only else []) + (["--rerecord"] if rerecord else [])
        for f in files or []:
            argv += ["--file", f]
        return rerun_elevated(argv, out / "_work" / "_导出日志.txt")
    return export_all(raw, out, pending, rerecord)


def main() -> int:
    ap = argparse.ArgumentParser(description="天正图转天正3：普查 + 记录 + TSAVEAS 导出")
    ap.add_argument("raw", type=Path, help="原始图纸目录（只读）")
    ap.add_argument("out", type=Path, help="产物目录，镜像 RAW 的目录结构")
    ap.add_argument("--only", default="", help="只处理路径里含这些子串的图，逗号分隔")
    ap.add_argument("--rerecord", action="store_true", help="只重写 <名>_tch.json，不重新导出天正3，已导出的也重写")
    ap.add_argument("--file", action="append", help="只处理这一张（相对 RAW目录 的路径），可以给多次；不扫整个目录")
    ap.add_argument("--log", type=Path, help=argparse.SUPPRESS)
    a = ap.parse_args()
    if a.log:
        sys.stdout = sys.stderr = a.log.open("w", encoding="utf-8", buffering=1)
    else:
        sys.stdout.reconfigure(encoding="utf-8")
    raw, out = a.raw.resolve(), a.out.resolve()
    if not raw.is_dir():
        print(f"找不到图纸目录: {raw}")
        return 1
    return convert(raw, out, a.only, a.rerecord, a.file)


if __name__ == "__main__":
    raise SystemExit(main())
