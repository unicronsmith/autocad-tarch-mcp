"""本机环境：AutoCAD 和天正装在哪、用哪个注册表键和 COM ProgID、数据放哪。

换一台机器不用改源码：安装位置从注册表读，读不到或要指定别的版本时用环境变量覆盖。

    ACAD_DIR      AutoCAD 安装目录（里面有 accoreconsole.exe）
    TARCH_DIR     天正建筑安装目录（里面有 TGStart.exe）
    ACADMCP_ROOT  MCP server 的数据根目录，相对路径和产物都落在这下面；不设就是启动时的当前目录
"""
from __future__ import annotations

import os
import re
import winreg
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Acad:
    dir: Path
    release: str   # 注册表里的发行号，如 R25.0
    product: str   # 产品键，如 ACAD-8101:804（804 是简体中文）
    name: str

    @property
    def major(self) -> int:
        return int(re.match(r"R(\d+)", self.release).group(1))

    @property
    def reg(self) -> str:
        """HKCU 下这个产品的键；配置（Profiles）在它下面。"""
        return rf"Software\Autodesk\AutoCAD\{self.release}\{self.product}"

    @property
    def progid(self) -> str:
        # 提权进程只认 HKLM 里的 COM 注册，版本无关的 AutoCAD.Application 只注册在 HKCU，所以带版本号
        return f"AutoCAD.Application.{self.major}"


@dataclass(frozen=True)
class Tarch:
    dir: Path
    version: str   # 注册表里的版本键，如 30V1

    @property
    def profile(self) -> str:
        """TGStart 拉起 AutoCAD 时用的配置名（/p 参数），T30 V1.0 是 TArch30V1。"""
        return f"TArch{self.version}"

    @property
    def tgstart(self) -> Path:
        return self.dir / "TGStart.exe"


def _subkeys(root, path: str) -> list[str]:
    try:
        with winreg.OpenKey(root, path) as k:
            return [winreg.EnumKey(k, i) for i in range(winreg.QueryInfoKey(k)[0])]
    except OSError:
        return []


def _value(root, path: str, name: str) -> str:
    try:
        with winreg.OpenKey(root, path) as k:
            return str(winreg.QueryValueEx(k, name)[0])
    except OSError:
        return ""


def acad_installs() -> list[Acad]:
    """本机装的全部 AutoCAD 完整版（有 accoreconsole.exe 的），新版本在前。"""
    base = r"SOFTWARE\Autodesk\AutoCAD"
    found = []
    for release in _subkeys(winreg.HKEY_LOCAL_MACHINE, base):
        for product in _subkeys(winreg.HKEY_LOCAL_MACHINE, rf"{base}\{release}"):
            key = rf"{base}\{release}\{product}"
            loc = _value(winreg.HKEY_LOCAL_MACHINE, key, "AcadLocation")
            if loc and (Path(loc) / "accoreconsole.exe").exists():
                found.append(Acad(Path(loc), release, product, _value(winreg.HKEY_LOCAL_MACHINE, key, "ProductName")))
    return sorted(found, key=lambda a: a.major, reverse=True)


def find_acad() -> Acad | None:
    installs = acad_installs()
    want = os.environ.get("ACAD_DIR")
    if not want:
        return installs[0] if installs else None
    want_dir = Path(want)
    for a in installs:
        if a.dir.resolve() == want_dir.resolve():
            return a
    # 指定的目录没在注册表里登记（便携安装之类）：发行号从目录名里的年份推，2025 → R25.0
    m = re.search(r"20(\d\d)", want_dir.name)
    return Acad(want_dir, f"R{m.group(1)}.0" if m else "R25.0", "ACAD-8101:804", want_dir.name)


def find_tarch() -> Tarch | None:
    want = os.environ.get("TARCH_DIR")
    base = r"SOFTWARE\Tangent\TArch"
    found = []
    for version in _subkeys(winreg.HKEY_LOCAL_MACHINE, base):
        loc = _value(winreg.HKEY_LOCAL_MACHINE, rf"{base}\{version}", "Location")
        if loc and (Path(loc) / "TGStart.exe").exists():
            found.append(Tarch(Path(loc), version))
    if want:
        for t in found:
            if t.dir.resolve() == Path(want).resolve():
                return t
        m = re.search(r"T(\d+V\d+)", Path(want).name)
        return Tarch(Path(want), m.group(1) if m else "30V1")
    return max(found, key=lambda t: t.version) if found else None


ACAD = find_acad() or Acad(Path(r"C:\Program Files\Autodesk\AutoCAD 2025"), "R25.0", "ACAD-8101:804", "")
TARCH = find_tarch() or Tarch(Path(r"C:\Tangent\TArchT30V1"), "30V1")
LOCAL = Path(os.environ.get("LOCALAPPDATA", r"C:\Temp")) / "acadmcp"
ROOT = Path(os.environ.get("ACADMCP_ROOT") or Path.cwd()).resolve()
