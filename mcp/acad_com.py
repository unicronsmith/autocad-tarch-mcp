"""通过 COM 驱动 AutoCAD GUI 实例，补无头通道做不到的图元。

目前只有 DIMENSION 需要走这里：accoreconsole 里 DIMALIGNED/DIMLINEAR 命令会
返回成功，但存盘读回图中标注数为 0 —— 无头内核根本不执行标注命令。

这条通道会占用用户正开着的 AutoCAD，所以有两条硬规矩：

1. 每个 COM 调用都要退避重试。AutoCAD 忙时返回 RPC_E_CALL_REJECTED，
   这不是失败，是"稍后再问"。不重试就会随机炸。
2. 只动自己打开的图纸。进来时记下用户已开的文档，出去时原样留着。

完整版才有这条通道，LT 上 AutoCAD.Application 根本不注册。
"""
from __future__ import annotations

import time
from pathlib import Path

try:
    import pythoncom
    import pywintypes
    import win32com.client
except ImportError as exc:  # pragma: no cover - 环境缺 pywin32 时由调用方降级
    raise ImportError("COM 通道需要 pywin32：pip install pywin32") from exc

# AutoCAD 忙时的两个 HRESULT，退避重试即可，不是真错误
BUSY_HRESULTS = (-2147418111, -2147417846)

RETRY_TRIES = 240
RETRY_DELAY = 0.25

# 对象未就绪时的重试上限（普通调用 30s）。
NOTREADY_TRIES = 120

# live() 专用的等待上限，480 × RETRY_DELAY = 120s。要这么长是因为试用期许可
# 的联网校验：实测冷启动后 Documents.Open 到 ActiveDocument 可访问卡了 50.4s，
# 正是那个"欢迎试用"弹窗出现的时刻。正式授权后不会这么久，但留着不花代价 ——
# 对象一就绪立刻返回，这只是上限不是固定等待。
READY_TRIES = 480


class ComError(RuntimeError):
    pass


def retry(fn, tries: int = RETRY_TRIES, delay: float = RETRY_DELAY,
          notready_tries: int = NOTREADY_TRIES):
    last = None
    notready = 0
    for _ in range(tries):
        try:
            return fn()
        except pywintypes.com_error as exc:
            codes = {exc.hresult, exc.args[0] if exc.args else None}
            if codes & set(BUSY_HRESULTS):
                last = exc
                time.sleep(delay)
                continue
            raise
        except AttributeError as exc:
            # 未就绪的 AutoCAD 对象在 pywin32 late-binding 下解析不出成员，抛的是
            # AttributeError("<unknown>.Count") 而不是 com_error —— 冷启动的 app
            # 和刚 Open 出来的 ActiveDocument 都会这样。late-binding 区分不了
            # "还没准备好" 和 "属性名拼错"，所以给它独立的较短窗口，超了抛原样。
            notready += 1
            if notready > notready_tries:
                raise
            last = exc
            time.sleep(delay)
    raise ComError(f"AutoCAD 持续忙，重试 {tries} 次仍被拒: {last}")


def live(get, probe: str):
    """取一个 COM 对象，并就地读一次 probe 成员确认它真的可用。

    未就绪的对象重试读它的成员永远不会变好 —— 坏的是这个引用本身，要整个重新
    取。所以探活必须和取对象在同一个 lambda 里，让 retry 重跑 get。
    """
    def once():
        obj = get()
        getattr(obj, probe)
        return obj

    return retry(once, tries=READY_TRIES, notready_tries=READY_TRIES)


def point(p) -> object:
    vals = [float(v) for v in list(p)[:3]]
    vals += [0.0] * (3 - len(vals))
    return win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, vals)


def connect() -> tuple[object, bool]:
    """连上运行中的 AutoCAD；没有就起一个隐藏实例。

    返回 (app, 是否由本函数启动)。由本函数启动的实例，调用方用完要 Quit。
    """
    try:
        return retry(lambda: win32com.client.GetActiveObject("AutoCAD.Application"), tries=4), False
    except (pywintypes.com_error, ComError):
        pass
    app = win32com.client.Dispatch("AutoCAD.Application")
    retry(lambda: setattr(app, "Visible", False))
    return app, True


def _add_dimension(msp, spec: dict):
    kind = str(spec.get("dim", "aligned")).lower()
    if kind == "aligned":
        return msp.AddDimAligned(point(spec["p1"]), point(spec["p2"]), point(spec["text_pos"]))
    if kind in ("linear", "rotated"):
        return msp.AddDimRotated(
            point(spec["p1"]), point(spec["p2"]), point(spec["text_pos"]),
            float(spec.get("angle", 0.0)),
        )
    if kind == "radial":
        return msp.AddDimRadial(
            point(spec["center"]), point(spec["chord_point"]), float(spec.get("leader", 1.0))
        )
    if kind == "diametric":
        return msp.AddDimDiametric(
            point(spec["chord_point"]), point(spec["far_chord_point"]),
            float(spec.get("leader", 1.0)),
        )
    if kind == "angular":
        return msp.AddDimAngular(
            point(spec["vertex"]), point(spec["p1"]), point(spec["p2"]), point(spec["text_pos"])
        )
    raise ComError(f"不支持的标注类型: {kind}（可用 aligned/linear/radial/diametric/angular）")


def add_dimensions(dwg: Path | str, specs: list[dict], timeout: float = 300.0) -> dict:
    """打开 dwg，加标注，保存后关闭。返回 {'added': n, 'types': [...]}。"""
    dwg = Path(dwg).resolve()
    if not dwg.exists():
        raise ComError(f"图纸不存在: {dwg}")
    if not specs:
        return {"added": 0, "types": []}

    t0 = time.time()
    bak = dwg.with_suffix(".bak")
    bak_existed = bak.exists()
    app, owned = connect()
    deadline = t0 + timeout
    opened = None
    try:
        docs = live(lambda: app.Documents, "Count")
        before = {
            retry(lambda i=i: docs.Item(i).FullName)
            for i in range(retry(lambda: docs.Count))
        }
        if str(dwg) in before:
            raise ComError(f"该图纸已在 AutoCAD 中打开，拒绝改动：{dwg}")

        handle = retry(lambda: docs.Open(str(dwg)))

        def _target():
            """认准目标图纸本身，不靠 ActiveDocument。

            冷启动时 ActiveDocument 迟迟返回未就绪的 <unknown>，实测等满 120s
            也不好转；Open 的返回值和 Documents 集合都能直接认到那一份。
            """
            if handle is not None:
                try:
                    if handle.FullName == str(dwg):
                        return handle
                except (AttributeError, pywintypes.com_error):
                    pass
            for i in range(docs.Count):
                d = docs.Item(i)
                if d.FullName == str(dwg):
                    return d
            raise AttributeError(f"目标图纸还没出现在 Documents 集合: {dwg}")

        opened = live(_target, "FullName")

        msp = live(lambda: opened.ModelSpace, "Count")
        layers = live(lambda: opened.Layers, "Count")
        made = []
        for spec in specs:
            if time.time() > deadline:
                raise ComError(f"超时 {timeout}s，已加 {len(made)} 个标注")
            obj = retry(lambda s=spec: _add_dimension(msp, s))
            name = spec.get("layer")
            if name:
                retry(lambda n=name: layers.Add(n))
                retry(lambda o=obj, n=name: setattr(o, "Layer", n))
            if spec.get("text"):
                retry(lambda o=obj, t=spec["text"]: setattr(o, "TextOverride", t))
            made.append(retry(lambda o=obj: o.ObjectName))

        retry(lambda: opened.Save())
        return {"added": len(made), "types": made, "elapsed": round(time.time() - t0, 1)}
    finally:
        if opened is not None:
            try:
                retry(lambda: opened.Close(False), tries=40)
            except Exception:
                pass
        if owned:
            try:
                retry(lambda: app.Quit(), tries=20)
            except Exception:
                pass
        # Save() 会把上一版留成 .bak。改 ISAVEBAK 能免掉，但那是全局系统变量，
        # 动它等于改用户的 AutoCAD 配置，所以只清自己弄出来的这一个。
        if not bak_existed and bak.exists():
            bak.unlink(missing_ok=True)


def probe() -> dict:
    """探测 COM 通道是否可用，给 selftest 和排障用。"""
    try:
        app, owned = connect()
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    try:
        return {
            "ok": True,
            "version": retry(lambda: app.Version),
            "exe": retry(lambda: app.FullName),
            "documents": retry(lambda: app.Documents.Count),
            "launched_by_probe": owned,
        }
    finally:
        if owned:
            try:
                retry(lambda: app.Quit(), tries=20)
            except Exception:
                pass
