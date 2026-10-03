"""环境体检：这台机器能不能跑。逐项打印 通过 / 缺 / 提示，缺了什么照原样说。

    python doctor.py [--quick]

必需项（缺一项退出码就是 1）：Python 依赖、AutoCAD 完整版、无头通道实跑一次。
天正项单列：缺了只是不能识别天正图，别的功能照常。--quick 不实跑 AutoCAD。
"""
from __future__ import annotations

import argparse
import importlib
import json
import shutil
import subprocess
import sys
import winreg
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import env

DEPS = {"ezdxf": "ezdxf", "win32com.client": "pywin32", "pywinauto": "pywinauto", "mcp.server.fastmcp": "mcp",
        "olefile": "olefile", "openpyxl": "openpyxl"}
MCP_NAME = "autocad"


class Report:
    def __init__(self) -> None:
        self.failed = 0
        self.tarch_failed = 0

    def ok(self, what: str, detail: str = "") -> None:
        print(f"  [通过] {what}{'  ' + detail if detail else ''}")

    def bad(self, what: str, fix: str, tarch: bool = False) -> None:
        print(f"  [ 缺 ] {what}\n         {fix}")
        if tarch:
            self.tarch_failed += 1
        else:
            self.failed += 1

    def note(self, what: str) -> None:
        print(f"  [提示] {what}")


def key_exists(root, path: str) -> bool:
    try:
        winreg.CloseKey(winreg.OpenKey(root, path))
        return True
    except OSError:
        return False


def check_python(r: Report) -> None:
    print("Python")
    v = sys.version_info
    if v >= (3, 10):
        r.ok(f"Python {v.major}.{v.minor}.{v.micro}", sys.executable)
    else:
        r.bad(f"Python {v.major}.{v.minor} 太旧", "装 3.10 以上：winget install Python.Python.3.12")
    for mod, pkg in DEPS.items():
        try:
            importlib.import_module(mod)
            r.ok(pkg)
        except ImportError as ex:
            r.bad(f"{pkg}（{ex}）", "python -m pip install -r requirements.txt")


def check_acad(r: Report, quick: bool) -> None:
    print("AutoCAD")
    acad_info = env.find_acad()
    if not acad_info or not (acad_info.dir / "accoreconsole.exe").exists():
        r.bad("没找到带 accoreconsole.exe 的 AutoCAD 完整版（LT 不行）",
              "装 AutoCAD 完整版；装在非默认位置就设环境变量 ACAD_DIR=<安装目录>")
        return
    r.ok(acad_info.name or acad_info.dir.name, str(acad_info.dir))
    if not acad_info.product.endswith(":804"):
        r.note(f"产品键 {acad_info.product} 不是简体中文版（:804）。出图的图纸尺寸名、脚本编码都按简体中文版写的，没在别的语言版上测过")
    if (acad_info.dir / "Fonts" / "gbcbig.shx").exists():
        r.ok("gbcbig.shx（缺大字体时拿它顶替）")
    else:
        r.bad("AutoCAD 的 Fonts 目录里没有 gbcbig.shx", "修复安装 AutoCAD；没有它，缺大字体的旧图中文会读成乱码")
    if quick:
        return
    import acad
    res = acad.run_lisp("(princ)")
    if res.ok:
        r.ok("无头通道实跑", f"{res.elapsed}s")
    else:
        r.bad(f"无头通道跑不通：{res.error}", f"日志尾部：{res.log[-300:]}")


def check_tarch(r: Report) -> None:
    print("天正（识别天正图才需要）")
    tarch, acad_info = env.find_tarch(), env.find_acad()
    if not tarch or not tarch.tgstart.exists():
        r.bad("没找到天正建筑正式版（TGStart.exe）",
              "装天正建筑 T30；装在非默认位置就设 TARCH_DIR=<安装目录>。免费的 T30 插件不行，它不能导出天正3", tarch=True)
        return
    r.ok(f"天正建筑 {tarch.version}", str(tarch.dir))
    if not acad_info:
        return
    if key_exists(winreg.HKEY_CURRENT_USER, rf"{acad_info.reg}\Profiles\{tarch.profile}"):
        r.ok(f"AutoCAD 配置 {tarch.profile}")
    else:
        r.bad(f"{acad_info.name or 'AutoCAD'} 里没有配置 {tarch.profile}",
              "手动启动一次天正建筑，启动时选这个版本的 AutoCAD，进到绘图界面再退出", tarch=True)
    if key_exists(winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\Classes\{acad_info.progid}"):
        r.ok(f"COM {acad_info.progid}")
    else:
        r.bad(f"HKLM 里没有注册 COM {acad_info.progid}", "修复安装 AutoCAD，或以管理员身份跑一次 acad.exe /regserver", tarch=True)
    check_plugin(r, acad_info)
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System") as k:
            if winreg.QueryValueEx(k, "ConsentPromptBehaviorAdmin")[0] != 0:
                r.note("导出天正3 要提权：每次批量导出开头会弹一次 UAC 确认框，要有人点「是」")
    except OSError:
        pass
    r.note("导出期间屏幕上会出现天正 AutoCAD 窗口，别去点它；自己开着 AutoCAD 时不要跑导出")


def tfm_of(config: Path) -> tuple[int, ...]:
    """runtimeconfig.json 里的目标框架，net10.0 → (10, 0)；读不出来是 ()。"""
    try:
        tfm = json.loads(config.read_text(encoding="utf-8"))["runtimeOptions"]["tfm"]
        return tuple(int(x) for x in tfm.removeprefix("net").split("."))
    except (OSError, KeyError, ValueError):
        return ()


def check_plugin(r: Report, acad_info: env.Acad) -> None:
    """tchdump 插件：在天正进程里分解对象、读属性的那个 DLL，得跟 AutoCAD 的 .NET 版本配得上。"""
    folder = Path(__file__).resolve().parent / "tchdump"
    dll = folder / f"R{acad_info.major}" / "TchDump.dll"
    host = tfm_of(acad_info.dir / "acdbmgd.runtimeconfig.json")
    dotted = lambda v: ".".join(map(str, v))
    build = (f'装 .NET SDK 后在 {folder} 下跑：dotnet build -c Release -p:AcadDir="{acad_info.dir}" '
             f"-p:AcadTfm=net{dotted(host)} -p:OutputPath=R{acad_info.major}/")
    if not dll.exists():
        hint = build if host else "这个版本的 AutoCAD 托管接口是 .NET Framework，插件工程编不了，天正图识别不了"
        r.bad(f"没有给 AutoCAD R{acad_info.major} 编好的 tchdump 插件", hint, tarch=True)
        return
    built = tfm_of(dll.with_suffix(".runtimeconfig.json"))
    if host and built and built > host:
        r.bad(f"tchdump 插件是按 .NET {dotted(built)} 编的，这台机器的 AutoCAD 是 .NET {dotted(host)}，加载不了", build, tarch=True)
    else:
        r.ok("tchdump 插件", f"R{acad_info.major}，.NET {dotted(built) or '?'}")


def check_claude(r: Report, name: str = MCP_NAME) -> None:
    print("Claude Code")
    exe = shutil.which("claude")
    if not exe:
        r.note("没找到 claude 命令。只用命令行脚本可以不装；要让 Claude 直接读图就装："
               "irm https://claude.ai/install.ps1 | iex")
        return
    try:
        ver = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=60).stdout.strip()
        got = subprocess.run([exe, "mcp", "get", name], capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as ex:
        r.note(f"claude 命令调不起来：{ex}")
        return
    r.ok(f"Claude Code {ver}")
    if got.returncode == 0 and "server.py" in got.stdout:
        r.ok(f"MCP server「{name}」已注册")
    else:
        r.note(f"MCP server「{name}」还没注册，跑 install.ps1，或者："
               f'claude mcp add --scope user {name} -- python "{Path(__file__).resolve().parent / "server.py"}"')


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="环境体检")
    ap.add_argument("--quick", action="store_true", help="不实跑 AutoCAD")
    ap.add_argument("--mcp-name", default=MCP_NAME, help="MCP server 的注册名")
    a = ap.parse_args()
    r = Report()
    check_python(r)
    if not r.failed:
        check_acad(r, a.quick)
    check_tarch(r)
    check_claude(r, a.mcp_name)
    print()
    if r.failed:
        print(f"结论：缺 {r.failed} 项必需的，先补上再用")
    elif r.tarch_failed:
        print(f"结论：读图、画图可用；天正项缺 {r.tarch_failed} 项，天正图暂时识别不了")
    else:
        print("结论：全部可用")
    return 1 if r.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
