"""驱动 AutoCAD 无头执行 AutoLISP。

用 accoreconsole.exe 而不是 acad.exe：它是真无头的控制台宿主，不开窗口、
不抢焦点、跑完自己退出，一轮约 1.6 秒；用 GUI 那条路要约 10 秒，还会和
用户正开着的实例抢资源。

    accoreconsole.exe [/i 图纸] /s 脚本.scr

不给 /i 就在空白图纸上执行，正好用于生成新图。每次调用是独立进程，无状态。

踩过的四个坑，改动前先读：

1. .scr 的每一行都独立送进命令行，跨行的 LISP 表达式会被切碎并让宿主挂住
   等括号。所以 LISP 一律用 flatten_lisp 压成单行。
2. 不能用 (load "x.lsp") 投递代码：SECURELOAD 默认为 1，加载受信任目录之外
   的 .lsp 会弹"未验证发布者"对话框把无头脚本挂死。本地 .scr 不在 SECURELOAD
   管辖范围内，所以内联进 .scr 可以完全绕开。
3. 简体中文版按 GBK 读脚本文件，用 ASCII/UTF-8 写会把中文路径变成 `?`。
   临时文件放纯 ASCII 目录，内容一律按 mbcs 写。
4. 交互式命令（SAVEAS 之类）必须走 post_lines 的原生脚本行，一行一次输入；
   用 (command "_.SAVEAS" "" path) 喂提示会吃不到而把脚本挂死。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import env

ACAD_DIR = env.ACAD.dir
ACCORE = ACAD_DIR / "accoreconsole.exe"
ACAD_GUI = ACAD_DIR / "acad.exe"
WORK = env.LOCAL / "work"
# accoreconsole 每跑一次都会把注册表 FixedProfile 里共享的 FILEDIA 写成 0，用户的 GUI 从此打开/保存不弹文件对话框
# （2025 实测：跑前 1、跑后 0，脚本本身没碰 FILEDIA）。/isolate 让它用这里的独立用户数据，注册表保持不动，耗时不变
ISOLATE = WORK.parent / "isolate"
# R2004 及更早的图按 GBK 字节存中文，读图时才转 Unicode：样式的大字体找不到、而搜索路径里又有 hztxt.shx 时，
# 这些字按单字节解成乱码（"板面高差"→"°?Ã?¸?²?"，后半字节丢失）；样式指定 hztxt 而它找不到时同样乱码。
# 2026-10-01 实测只要每个大字体都找得到就全对，用哪个 GBK 大字体文件都一样，所以缺的一律拿 gbcbig.shx
# 改名放进这里，accoreconsole 以它为启动目录（AutoCAD 会在启动目录里找字体）
FONTS = WORK.parent / "fonts"
GBCBIG = ACAD_DIR / "Fonts" / "gbcbig.shx"
DEFAULT_TIMEOUT = 120


class AcadError(RuntimeError):
    pass


@dataclass
class Result:
    ok: bool
    elapsed: float
    outputs: dict[str, str] = field(default_factory=dict)
    error: str = ""
    log: str = ""
    stats: dict[str, int] = field(default_factory=dict)


def lisp_path(p: Path | str) -> str:
    return str(p).replace("\\", "/")


def lisp_str(s: Path | str) -> str:
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _write_gbk(p: Path, text: str) -> None:
    p.write_bytes(text.encode("mbcs", errors="replace"))


def _read_any(p: Path) -> str:
    raw = p.read_bytes()
    for enc in ("utf-8", "mbcs"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("mbcs", errors="replace")


ERROR_HOOK = """(defun *error* (msg / ef)
  (setq ef (open %ERR% "w"))
  (write-line (if msg msg "unknown") ef)
  (close ef)
  (princ))
"""


def flatten_lisp(code: str) -> list[str]:
    """按顶层括号把 LISP 源码切成一行一个表达式。

    .scr 的每一行独立送进命令行，多行表达式会被切碎并让 AutoCAD 挂住等
    括号；而走 (load) 加载 .lsp 又会撞上 SECURELOAD 的"未签名可执行文件"
    弹框，无头脚本同样挂死。把表达式压成单行直接喂命令行可以两头都绕开。
    """
    exprs: list[str] = []
    buf: list[str] = []
    depth = 0
    in_str = False
    esc = False
    i = 0
    while i < len(code):
        ch = code[i]
        if in_str:
            buf.append(ch)
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == ";":
            while i < len(code) and code[i] != "\n":
                i += 1
            continue
        elif ch == '"':
            in_str = True
            buf.append(ch)
        elif ch == "(":
            depth += 1
            buf.append(ch)
        elif ch == ")":
            depth -= 1
            buf.append(ch)
            if depth == 0:
                exprs.append("".join(buf))
                buf = []
        elif ch in " \t\r\n":
            if depth > 0 and buf and buf[-1] not in " (":
                buf.append(" ")
        else:
            buf.append(ch)
        i += 1
    if depth != 0:
        raise AcadError(f"LISP 括号不配平，残留深度 {depth}")
    return [e for e in exprs if e.strip()]


def run_lisp(
    lisp_code: str,
    drawing: Path | str | None = None,
    reads: dict[str, Path] | None = None,
    post_lines: list[str] | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    keep: bool = False,
) -> Result:
    """把 lisp_code 写成 .lsp 交给无头 AutoCAD 执行。

    lisp_code 可以自由换行。post_lines 是 LISP 跑完后追加到 .scr 的原生脚本
    行，每行算一次命令行输入 —— 交互式命令（SAVEAS 之类）必须走这里，用
    (command ...) 喂提示会吃不到而把脚本挂死。

    ok=True 的判据是脚本走到最后写出完成标记，中途报错会如实反映，LISP 的
    报错原文通过 *error* 钩子回传到 Result.error。
    """
    if not ACCORE.exists():
        raise AcadError(f"找不到 accoreconsole: {ACCORE}")

    WORK.mkdir(parents=True, exist_ok=True)
    stamp = uuid.uuid4().hex[:12]
    scr = WORK / f"{stamp}.scr"
    done = WORK / f"{stamp}.done"
    staged = WORK / f"{stamp}.staged"
    err = WORK / f"{stamp}.err"

    body = ERROR_HOOK.replace("%ERR%", lisp_str(lisp_path(err))) + "\n" + lisp_code
    scr_lines: list[str] = []
    scr_lines += flatten_lisp(body)
    scr_lines.append(
        f'(setq _s (open {lisp_str(lisp_path(staged))} "w"))(write-line "ok" _s)(close _s)'
    )
    scr_lines += list(post_lines or [])
    scr_lines.append(
        f'(setq _d (open {lisp_str(lisp_path(done))} "w"))(write-line "ok" _d)(close _d)'
    )
    _write_gbk(scr, "\r\n".join(scr_lines) + "\r\n")

    args = [str(ACCORE), "/isolate", "acadmcp", str(ISOLATE)]
    if drawing is not None:
        args += ["/i", str(Path(drawing).resolve())]
    args += ["/s", str(scr)]

    FONTS.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=str(FONTS))
    try:
        raw, _ = proc.communicate(timeout=timeout)
        timed_out = False
    except subprocess.TimeoutExpired:
        proc.kill()
        raw, _ = proc.communicate()
        timed_out = True

    ok = done.exists()
    error = _read_any(err).strip() if err.exists() else ""
    lisp_reached_end = staged.exists()

    outputs = {
        key: (_read_any(Path(path)) if Path(path).exists() else "")
        for key, path in (reads or {}).items()
    }
    if not ok and not error:
        if timed_out:
            error = (
                "超时：LISP 已跑完，卡在后续命令行阶段（post_lines）"
                if lisp_reached_end
                else "超时：LISP 没跑到结尾"
            )
        else:
            error = (
                "宿主已退出但没写出完成标记；卡在后续命令行阶段（post_lines）"
                if lisp_reached_end
                else "宿主已退出但 LISP 没跑到结尾"
            )

    res = Result(
        ok=ok,
        elapsed=round(time.time() - t0, 1),
        outputs=outputs,
        error=error,
        log=_decode_console(raw) if not ok else "",
    )
    if not keep:
        for p in (scr, done, staged, err):
            p.unlink(missing_ok=True)
    return res


def _decode_console(raw: bytes | None, limit: int = 2500) -> str:
    """解码 accoreconsole 的 stdout。

    它按 UTF-16LE 输出，直接当窄字符读会得到字符间夹空字节的乱码。
    """
    if not raw:
        return ""
    if raw[:400].count(0) > len(raw[:400]) // 4:
        text = raw.decode("utf-16-le", errors="replace")
    else:
        text = raw.decode("mbcs", errors="replace")
    return text.replace("\x00", "")[-limit:]


SURVEY_LISP = r"""
(defun bump (tbl key / hit)
  (setq hit (assoc key tbl))
  (if hit (subst (cons key (1+ (cdr hit))) hit tbl) (cons (cons key 1) tbl)))

(defun survey (outpath / f ss n i ed k kinds lays blks lay blk)
  (setq f (open outpath "w"))
  (setq ss (ssget "_X"))
  (setq n (if ss (sslength ss) 0))
  (write-line (strcat "TOTAL\t" (itoa n)) f)
  (setq i 0 kinds nil lays nil blks nil)
  (while (< i n)
    (setq ed (entget (ssname ss i)))
    (setq k (cdr (assoc 0 ed)))
    (setq lay (cdr (assoc 8 ed)))
    (setq kinds (bump kinds k))
    (if lay (setq lays (bump lays lay)))
    (if (= k "INSERT")
      (progn (setq blk (cdr (assoc 2 ed))) (if blk (setq blks (bump blks blk)))))
    (setq i (1+ i)))
  (foreach p kinds (write-line (strcat "KIND\t" (car p) "\t" (itoa (cdr p))) f))
  (foreach p lays (write-line (strcat "LAYER\t" (car p) "\t" (itoa (cdr p))) f))
  (foreach p blks (write-line (strcat "BLOCK\t" (car p) "\t" (itoa (cdr p))) f))
  (write-line (strcat "EXTMIN\t" (rtos (car (getvar "EXTMIN")) 2 4) "\t"
                      (rtos (cadr (getvar "EXTMIN")) 2 4)) f)
  (write-line (strcat "EXTMAX\t" (rtos (car (getvar "EXTMAX")) 2 4) "\t"
                      (rtos (cadr (getvar "EXTMAX")) 2 4)) f)
  (close f))

(survey %OUT%)
"""


# ssget "_X" 只给顶层主图元：ATTRIB 是 INSERT 的子图元，得顺着 entnext 走；块定义里的文字得走块表。
# 管网项目 2026-10-03 实测，只筛 TEXT/MTEXT 时 122 张漏掉块属性 69.7 万、块内文字 4.3 万、标注改写 3163、多重引线 154 条。
# 每行第一列是记录类型：T 顶层文字 / A 块属性 / D 标注改写 / M 多重引线 / B 块定义内文字 / N 块内嵌套的块参照 /
# I 块在顶层被引用的次数 / P 代理对象个数（有就再走 proxy_texts）。
# 引用次数不能靠逐个遍历全部 INSERT 来数：区位图 12.7 万个块参照，遍历一遍实测 280s（ssname 和 ssnamex 都一样）。
# 所以只遍历带属性的 INSERT（3.3 万个约 8s），次数用按块名筛的 ssget 取 sslength，而且只数含文字或含嵌套块参照的块。
TEXT_LISP = r"""
(defun mcp-esc (s / r i c)
  (setq r "" i 1)
  (while (<= i (strlen s))
    (setq c (substr s i 1))
    (setq r (strcat r (if (member c '("#" "@" "." "*" "?" "~" "[" "]" "-" "," "`")) (strcat "`" c) c)))
    (setq i (1+ i)))
  r)
(defun mcp-mtxt (ed / s p)
  (setq s "")
  (foreach p ed (if (= 3 (car p)) (setq s (strcat s (cdr p)))))
  (if (assoc 1 ed) (setq s (strcat s (cdr (assoc 1 ed)))))
  s)
(defun mcp-mltxt (ed / s p)
  (setq s "")
  (foreach p ed (if (and (= 304 (car p)) (= s "") (/= (cdr p) "LEADER_LINE{")) (setq s (cdr p))))
  s)
(defun mcp-top (f / ss n i ed s)
  (setq ss (ssget "_X" (list (cons 0 "TEXT,MTEXT"))))
  (setq n (if ss (sslength ss) 0) i 0)
  (while (< i n)
    (setq ed (entget (ssname ss i)) s (mcp-mtxt ed))
    (if (/= s "") (write-line (strcat "T\t" (cdr (assoc 8 ed)) "\t" (cdr (assoc 0 ed)) "\t" s) f))
    (setq i (1+ i))))
(defun mcp-ins (f / ss n i e ed bn)
  (setq ss (ssget "_X" (list (cons 0 "INSERT") (cons 66 1))))
  (setq n (if ss (sslength ss) 0) i 0)
  (while (< i n)
    (setq bn (cdr (assoc 2 (entget (ssname ss i)))) e (entnext (ssname ss i)))
    (while (and e (= "ATTRIB" (cdr (assoc 0 (setq ed (entget e))))))
      (if (/= "" (cdr (assoc 1 ed)))
        (write-line (strcat "A\t" (cdr (assoc 8 ed)) "\t" bn "\t" (cdr (assoc 2 ed)) "\t"
                            (itoa (logand 1 (cdr (assoc 70 ed)))) "\t" (cdr (assoc 1 ed))) f))
      (setq e (entnext e)))
    (setq i (1+ i))))
(defun mcp-ann (f / ss n i ed s)
  (setq ss (ssget "_X" (list (cons 0 "DIMENSION"))))
  (setq n (if ss (sslength ss) 0) i 0)
  (while (< i n)
    (setq ed (entget (ssname ss i)) s (cdr (assoc 1 ed)))
    (if (and s (/= s "") (/= s "<>")) (write-line (strcat "D\t" (cdr (assoc 8 ed)) "\t" s) f))
    (setq i (1+ i)))
  (setq ss (ssget "_X" (list (cons 0 "MULTILEADER"))))
  (setq n (if ss (sslength ss) 0) i 0)
  (while (< i n)
    (setq ed (entget (ssname ss i)) s (mcp-mltxt ed))
    (if (/= s "") (write-line (strcat "M\t" (cdr (assoc 8 ed)) "\t" s) f))
    (setq i (1+ i))))
(defun mcp-blk (f / b bn e ed et s hit ss k np)
  (setq b (tblnext "BLOCK" T) k 0 np 0)
  (while b
    (setq bn (cdr (assoc 2 b)))
    (if (and (not (wcmatch (strcase bn) "`*MODEL_SPACE*,`*PAPER_SPACE*,`*D#*"))
             (= 0 (logand 20 (cdr (assoc 70 b)))))
      (progn
        (setq e (cdr (assoc -2 b)) hit nil)
        (while e
          (setq ed (entget e) et (cdr (assoc 0 ed)) s "")
          (cond
            ((or (= et "TEXT") (= et "MTEXT")) (setq s (mcp-mtxt ed)))
            ((= et "ATTRIB") (setq s (cdr (assoc 1 ed))))
            ((and (= et "ATTDEF") (= 2 (logand 2 (cdr (assoc 70 ed))))) (setq s (cdr (assoc 1 ed))))
            ((= et "DIMENSION") (setq s (cdr (assoc 1 ed))) (if (or (not s) (= s "<>")) (setq s "")))
            ((= et "MULTILEADER") (setq s (mcp-mltxt ed)))
            ((= et "ACAD_PROXY_ENTITY") (setq np (1+ np)))
            ((= et "INSERT") (setq hit T) (write-line (strcat "N\t" bn "\t" (cdr (assoc 2 ed))) f)))
          (if (and s (/= s ""))
            (progn
              (setq hit T)
              (write-line (strcat "B\t" bn "\t" (cdr (assoc 8 ed)) "\t" et "\t"
                                  (if (member et '("ATTRIB" "ATTDEF")) (cdr (assoc 2 ed)) "") "\t" s) f)))
          (setq e (entnext e)))
        (if hit
          (progn
            (setq ss (ssget "_X" (list (cons 0 "INSERT") (cons 2 (mcp-esc bn)))))
            (write-line (strcat "I\t" bn "\t" (itoa (if ss (sslength ss) 0))) f)
            (setq ss nil k (1+ k))
            (if (= 0 (rem k 50)) (gc))))))
    (setq b (tblnext "BLOCK")))
  (write-line (strcat "P\t" (itoa np)) f))
(defun grab-text (outpath / f ss)
  (setq f (open outpath "w"))
  (mcp-top f)
  (mcp-ins f)
  (mcp-ann f)
  (mcp-blk f)
  (setq ss (ssget "_X" (list (cons 0 "ACAD_PROXY_ENTITY"))))
  (write-line (strcat "P\t" (itoa (if ss (sslength ss) 0))) f)
  (close f))

(grab-text %OUT%)
"""


XDATA_PROBE_LISP = r"""
(defun probe-apps (outpath / f apps ss n)
  (setq f (open outpath "w"))
  (setq apps (tblnext "APPID" T))
  (while apps
    (setq ss (ssget "_X" (list (list -3 (list (cdr (assoc 2 apps)))))))
    (setq n (if ss (sslength ss) 0))
    (if (> n 0)
      (write-line (strcat (cdr (assoc 2 apps)) "\t" (itoa n)) f))
    (setq apps (tblnext "APPID")))
  (close f))

(probe-apps %OUT%)
"""


XDATA_DUMP_LISP = r"""
(defun x2s (x)
  (cond
    ((= (type x) 'STR) x)
    ((= (type x) 'REAL) (rtos x 2 6))
    ((= (type x) 'INT) (itoa x))
    ((= (type x) 'LIST)
       (apply 'strcat (mapcar '(lambda (n) (strcat (if (numberp n) (rtos n 2 6) "?") ",")) x)))
    (T "")))

(defun collect (anchors types / flt)
  ;; 一次 OR 筛完所有锚点。逐个 ssget 再 ssadd 合并会在大图上慢到超时：
  ;; 全图扫 N 遍，还要逐个把几万个对象加进选择集。
  (setq flt (append
              (list (cons -4 "<OR"))
              (mapcar '(lambda (a) (list -3 (list a))) anchors)
              (list (cons -4 "OR>"))))
  (if (/= types "")
    (setq flt (append (list (cons -4 "<AND") (cons 0 types))
                      flt
                      (list (cons -4 "AND>")))))
  (ssget "_X" flt))

(defun dump-xd (outpath anchors types maxn / f ss n i ed line p10 p11)
  (setq f (open outpath "w"))
  (setq ss (collect anchors types))
  (setq n (if ss (sslength ss) 0))
  (write-line (strcat "#MATCHED=" (itoa n)) f)
  (setq i 0)
  (while (and (< i n) (< i maxn))
    (setq ed (entget (ssname ss i) (list "*")))
    (setq p10 (cdr (assoc 10 ed)) p11 (cdr (assoc 11 ed)))
    (setq line (strcat "#=" (cdr (assoc 5 ed))
                       "\t@=" (cdr (assoc 0 ed))
                       "\t~=" (cdr (assoc 8 ed))))
    (if p10 (setq line (strcat line "\t[=" (x2s p10))))
    (if p11 (setq line (strcat line "\t]=" (x2s p11))))
    (foreach p ed
      (if (= (car p) -3)
        (foreach app (cdr p)
          (foreach item (cdr app)
            (if (= (car item) 1000)
              (setq line (strcat line "\t" (car app) "=" (x2s (cdr item)))))))))
    (write-line line f)
    (setq i (1+ i)))
  (close f))

(dump-xd %OUT% %ANCHOR% %TYPES% %MAXN%)
"""


DUMP_LISP = r"""
(defun l2s (lst / acc)
  (setq acc "")
  (foreach n lst
    (setq acc (strcat acc (if (numberp n) (rtos n 2 8) "?") ",")))
  acc)

(defun v2s (x)
  (cond
    ((= (type x) 'STR) x)
    ((= (type x) 'REAL) (rtos x 2 8))
    ((= (type x) 'INT) (itoa x))
    ((= (type x) 'LIST) (l2s x))
    ((= (type x) 'ENAME) "<ename>")
    (T "<other>")))

(defun dump-all (outpath maxn / f ss i n ed line)
  (setq f (open outpath "w"))
  (setq ss (ssget "_X" %FILTER%))
  (if ss
    (progn
      (setq n (sslength ss))
      (write-line (strcat "#MATCHED=" (itoa n)) f)
      (setq i 0)
      (while (and (< i n) (< i maxn))
        (setq ed (entget (ssname ss i)))
        (setq line "")
        (foreach p ed
          (if (not (listp (car p)))
            (setq line (strcat line (itoa (car p)) "=" (v2s (cdr p)) "\t"))))
        (write-line line f)
        (setq i (1+ i))))
    (write-line "#MATCHED=0" f))
  (close f))

(dump-all %OUT% %MAXN%)
"""


def _require(drawing: Path | str) -> Path:
    p = Path(drawing)
    if not p.exists():
        raise AcadError(f"图纸不存在: {p}")
    return p


def _ssget_filter(types: str = "", layers: str = "") -> str:
    """拼 ssget 的过滤表。

    过滤必须下推到 ssget：一张 50MB 的地形图有近 50 万图元，先全量 dump
    再在 Python 侧筛，光中间文本就有几百 MB。组码 0 和 8 都支持逗号分隔
    多值和通配符。
    """
    parts = []
    if types:
        parts.append(f'(cons 0 {lisp_str(types.upper())})')
    if layers:
        parts.append(f'(cons 8 {lisp_str(layers)})')
    return "(list " + " ".join(parts) + ")" if parts else "nil"


def survey_drawing(drawing: Path | str, timeout: int = DEFAULT_TIMEOUT) -> tuple[Result, dict]:
    """只统计不导明细，任意大小的图纸都能跑。"""
    drawing = _require(drawing)
    WORK.mkdir(parents=True, exist_ok=True)
    out = WORK / f"survey-{uuid.uuid4().hex[:12]}.txt"
    code = SURVEY_LISP.replace("%OUT%", lisp_str(lisp_path(out)))
    res = run_lisp(code, drawing=drawing, reads={"s": out}, timeout=timeout)
    out.unlink(missing_ok=True)

    info: dict = {"总图元数": 0, "类型分布": {}, "图层分布": {}, "块引用": {}, "范围": None}
    lo: list[float] = []
    hi: list[float] = []
    for line in res.outputs.get("s", "").splitlines():
        cols = line.rstrip("\n").split("\t")
        if cols[0] == "TOTAL" and len(cols) > 1:
            info["总图元数"] = int(cols[1])
        elif cols[0] in ("KIND", "LAYER", "BLOCK") and len(cols) > 2:
            key = {"KIND": "类型分布", "LAYER": "图层分布", "BLOCK": "块引用"}[cols[0]]
            info[key][cols[1]] = int(cols[2])
        elif cols[0] == "EXTMIN" and len(cols) > 2:
            lo = [float(cols[1]), float(cols[2])]
        elif cols[0] == "EXTMAX" and len(cols) > 2:
            hi = [float(cols[1]), float(cols[2])]
    for k in ("类型分布", "图层分布", "块引用"):
        info[k] = dict(sorted(info[k].items(), key=lambda kv: -kv[1]))
    if lo and hi:
        info["范围"] = {"min": lo, "max": hi}
    return res, info


def _text_item(layer: str, kind: str, raw: str, **extra) -> dict:
    cleaned = clean_text(raw)
    it = {"图层": layer, "类型": kind, "文字": cleaned}
    if cleaned != raw:
        it["原文"] = raw
    it.update(extra)
    return it


def grab_text(drawing: Path | str, timeout: int = DEFAULT_TIMEOUT) -> tuple[Result, list[dict]]:
    """只抓文字，不碰几何。缺的大字体同 dxfout 一样补进 FONTS 再抓一遍。

    顶层 TEXT/MTEXT 排在前面，不带「来源」；其余带「来源」：块属性 / 标注改写 / 多重引线 / 块内 / 代理图形。
    块内文字每个块定义只出一条，「引用次数」是它在图面上出现的次数（嵌套按倍数算），0 表示图面上看不到。
    图里有代理对象时再走一遍 proxy_texts，计数记在 Result.stats。
    """
    drawing = _require(drawing)
    WORK.mkdir(parents=True, exist_ok=True)
    out = WORK / f"text-{uuid.uuid4().hex[:12]}.txt"
    probe = WORK / f"bigfont-{uuid.uuid4().hex[:12]}.txt"
    code = (BIGFONT_LISP + f"\n(mcp-bigfonts {lisp_str(lisp_path(probe))} nil)\n"
            + TEXT_LISP.replace("%OUT%", lisp_str(lisp_path(out))))
    for _ in range(2):
        res = run_lisp(code, drawing=drawing, reads={"t": out, "b": probe}, timeout=timeout)
        out.unlink(missing_ok=True)
        probe.unlink(missing_ok=True)
        if not alias_bigfonts(res.outputs.get("b", "").splitlines()):
            break

    item = _text_item
    top: list[dict] = []
    rest: list[dict] = []
    inner: list[dict] = []
    placed: Counter[str] = Counter()
    nested: dict[str, Counter[str]] = {}
    proxies = 0
    for line in res.outputs.get("t", "").splitlines():
        tag, _, body = line.partition("\t")
        if tag == "T":
            c = body.split("\t", 2)
            if len(c) == 3:
                top.append(item(c[0], c[1], c[2]))
        elif tag == "P":
            proxies += int(body) if body.isdigit() else 0
        elif tag == "I":
            c = body.split("\t", 1)
            if len(c) == 2 and c[1].isdigit():
                placed[c[0].upper()] = int(c[1])
        elif tag == "A":
            c = body.split("\t", 4)
            if len(c) == 5:
                it = item(c[0], "ATTRIB", c[4], 来源="块属性", 块=c[1], 标记=c[2])
                if c[3] == "1":
                    it["不可见"] = True
                rest.append(it)
        elif tag in ("D", "M"):
            c = body.split("\t", 1)
            if len(c) == 2:
                kind, src = ("DIMENSION", "标注改写") if tag == "D" else ("MULTILEADER", "多重引线")
                rest.append(item(c[0], kind, c[1], 来源=src))
        elif tag == "N":
            c = body.split("\t", 1)
            if len(c) == 2:
                nested.setdefault(c[1].upper(), Counter())[c[0].upper()] += 1
        elif tag == "B":
            c = body.split("\t", 4)
            if len(c) == 5:
                it = item(c[1], c[2], c[4], 来源="块内", 块=c[0])
                if c[3]:
                    it["标记"] = c[3]
                inner.append(it)

    shown: dict[str, int] = {}

    def times(block: str, path: tuple[str, ...] = ()) -> int:
        if block not in shown:
            if block in path:
                return 0
            shown[block] = placed[block] + sum(
                times(parent, path + (block,)) * n for parent, n in nested.get(block, {}).items())
        return shown[block]

    for it in inner:
        it["引用次数"] = times(it["块"].upper())
    drawn: list[dict] = []
    if res.ok and proxies:
        drawn, res.stats = proxy_texts(drawing)
    return res, top + rest + inner + drawn


def proxy_texts(drawing: Path | str, timeout: int = 600) -> tuple[list[dict], dict[str, int]]:
    """代理对象的代理图形里画出来的文字（grab_text 的条目格式），外加 proxy_objects 的计数。"""
    objects, stats = proxy_objects(drawing, timeout)
    items = [_text_item(t["图层"], "TEXT", t["文字"], 来源="代理图形", 类=o["类"],
                        **({"块": o["块"]} if "块" in o else {}))
             for o in objects for t in o["文字"]]
    return items, stats


def proxy_objects(drawing: Path | str, timeout: int = 600) -> tuple[list[dict], dict[str, int]]:
    """逐个代理对象解出它的代理图形：文字、线、圆，外加 {代理, 带图形, 解码中断} 计数。只返回带图形的对象。

    自定义对象存盘时带了代理图形才有东西可解：广联达鸿业三维管线的 CIVIL_* 带（井号、标高、管径标注、
    纵断面表头、井表都在里面），天正的 TCH_* 不带（得走 tarch_t3.py）。
    一个对象的文字是按图元拆开的（一个井标注是井号、标高、坐标几条），同一对象的归在一起。
    块定义里的代理对象带「块」，不算引用次数。坐标是世界坐标。
    """
    import ezdxf
    from ezdxf.proxygraphic import ProxyGraphic

    class Tolerant(ProxyGraphic):
        # ezdxf 1.4.4 对带圆弧段的多段线做非等比变换会抛错，这段代理图形后面的图元（含文字）跟着全丢。
        # 变换不了的把圆弧段拉直再交回去；管网项目有 8 个对象是这种情况
        def lwpolyline(self, data: bytes):
            e = super().lwpolyline(data)
            if e is not None and self.matrices and e.has_arc:
                try:
                    e.copy().transform(self.matrices[-1])
                except Exception:
                    e.set_points([(x, y, s, w, 0) for x, y, s, w, _ in e.get_points()])
            return e

    WORK.mkdir(parents=True, exist_ok=True)
    dxf = WORK / f"proxy-{uuid.uuid4().hex[:12]}.dxf"
    try:
        doc = ezdxf.readfile(dxfout(drawing, dxf, timeout=timeout))
        names = dxf_class_names(dxf)
    finally:
        dxf.unlink(missing_ok=True)
    objects: list[dict] = []
    stats = {"代理": 0, "带图形": 0, "解码中断": 0}

    def xy(p) -> list[float]:
        return [round(float(p[0]), 4), round(float(p[1]), 4)]

    for blk in doc.blocks:
        home = {} if blk.is_any_layout else {"块": blk.name}
        for e in blk.query("ACAD_PROXY_ENTITY"):
            stats["代理"] += 1
            # 组码 91 是类 ID：CLASSES 段第一个类是 500，往后顺延
            i = (e.acdb_proxy_entity.get_first_value(91, 0) if e.acdb_proxy_entity else 0) - 500
            obj = {"句柄": e.dxf.handle, "类": names[i] if 0 <= i < len(names) else "", "图层": e.dxf.layer,
                   **home, "文字": [], "线": [], "圆": []}
            n = 0
            try:
                for v in Tolerant(e.proxy_graphic or b"", doc).virtual_entities():
                    n += 1
                    kind = v.dxftype()
                    if kind == "TEXT" and v.dxf.text.strip():
                        obj["文字"].append({"图层": v.dxf.layer, "文字": v.dxf.text, "位置": xy(v.dxf.insert),
                                           "字高": round(float(v.dxf.height), 4),
                                           "转角": round(float(v.dxf.get("rotation", 0.0)), 4)})
                    elif kind == "LWPOLYLINE":
                        obj["线"].append([xy(p) for p in v.get_points("xy")])
                    elif kind == "POLYLINE":
                        obj["线"].append([xy(p) for p in v.points()])
                    elif kind == "CIRCLE":
                        obj["圆"].append([*xy(v.dxf.center), round(float(v.dxf.radius), 4)])
            except Exception:
                stats["解码中断"] += 1
                obj["解码中断"] = True
            if n:
                stats["带图形"] += 1
                objects.append(obj)
    return objects, stats


def dxf_class_names(dxf: Path | str) -> list[str]:
    """CLASSES 段里各个类的 DXF 名，按文件里的先后。代理对象的组码 91 是类 ID：第一个类是 500，往后顺延。

    不能数 ezdxf 的 doc.classes：同名的类它只留一个，后面的序号全部错位（2026-10-03 旧改项目一张图，
    1077 号类是 TCH_BLOCK_INSERT，按 doc.classes 数出来是别的类）。
    """
    names: list[str] = []
    section = want = ""
    with Path(dxf).open(encoding="utf-8", errors="replace") as f:
        for code, value in zip(f, f):
            code, value = code.strip(), value.strip()
            if code == "0":
                if value == "ENDSEC" and section == "CLASSES":
                    break
                want = "section" if value == "SECTION" else "class" if section == "CLASSES" and value == "CLASS" else ""
            elif code == "2" and want == "section":
                section, want = value, ""
            elif code == "1" and want == "class":
                names.append(value)
                want = ""
    return names


BIGFONT_LISP = r"""
(defun mcp-bigfonts (out all / f s b)
  (setq f (open out "w") s (tblnext "STYLE" T))
  (while s
    (setq b (cdr (assoc 4 s)))
    (if (and b (/= b "") (or all (not (or (findfile b) (findfile (strcat b ".shx"))))))
      (write-line b f))
    (setq s (tblnext "STYLE")))
  (close f)
  (princ))
"""


def _font_file(name: str) -> str:
    base = Path(name.strip()).name.lower()
    return base if base.endswith(".shx") else base + ".shx"


def alias_bigfonts(names: list[str], folder: Path = FONTS) -> list[str]:
    """缺的大字体拿 gbcbig.shx 改名补进 folder，返回这次新补的文件名。"""
    folder.mkdir(parents=True, exist_ok=True)
    made = []
    for name in sorted({_font_file(n) for n in names if n.strip()}):
        if not (folder / name).exists():
            shutil.copy2(GBCBIG, folder / name)
            made.append(name)
    return made


def bigfonts(drawing: Path | str, timeout: int = DEFAULT_TIMEOUT) -> list[str]:
    """图里文字样式引用的全部大字体文件名。"""
    out = WORK / f"bigfont-{uuid.uuid4().hex[:12]}.txt"
    code = BIGFONT_LISP + f"\n(mcp-bigfonts {lisp_str(lisp_path(out))} T)"
    res = run_lisp(code, drawing=_require(drawing), reads={"b": out}, timeout=timeout)
    out.unlink(missing_ok=True)
    if not res.ok:
        raise AcadError(f"读文字样式失败 {drawing}: {res.error}")
    return sorted({_font_file(n) for n in res.outputs.get("b", "").splitlines() if n.strip()})


def dxfout(drawing: Path | str, dst: Path | str, timeout: int = 600) -> Path:
    """dst 不能含空格：.scr 里空格等于回车，文件名会在 DXFOUT 的提示处被截断，后面的输入全部错位。

    字体在读图那一刻就定了中文怎么解码，所以导出时顺带查缺哪些大字体；有缺的就补进 FONTS 再导一遍。
    """
    dst = Path(dst)
    if " " in str(dst):
        raise AcadError(f"DXF 输出路径不能含空格: {dst}")
    drawing = _require(drawing)
    probe = WORK / f"bigfont-{uuid.uuid4().hex[:12]}.txt"
    code = '(setvar "FILEDIA" 0)\n' + BIGFONT_LISP + f"\n(mcp-bigfonts {lisp_str(lisp_path(probe))} nil)"
    for _ in range(2):
        dst.unlink(missing_ok=True)
        res = run_lisp(code, drawing=drawing, reads={"b": probe},
                       post_lines=["_.DXFOUT", lisp_path(dst), ""], timeout=timeout)
        probe.unlink(missing_ok=True)
        if not alias_bigfonts(res.outputs.get("b", "").splitlines()):
            break
    if not (res.ok and dst.exists()):
        raise AcadError(f"DXFOUT 失败 {drawing}: {res.error}\n{res.log[-800:]}")
    return dst


def salvage(drawing: Path | str, dst: Path | str, timeout: int = DEFAULT_TIMEOUT) -> Path:
    """救回 /i 打不开的损坏图（ErrorStatus=190）：炸开插入空白图再另存到 dst。

    只带回模型空间，布局里的内容没有；插入点取 0,0，原图基点 INSBASE 不在原点时坐标整体平移。
    无头 RECOVER 给了路径只会重新提示，RECOVERAUTO=2 对 /i 也不起作用（2026-10-03 拿一张损坏图
    实测），能用的只有这条路。
    """
    drawing = _require(drawing)
    dst = Path(dst).resolve()
    dst.parent.mkdir(parents=True, exist_ok=True)
    WORK.mkdir(parents=True, exist_ok=True)
    # .scr 里空格等于回车，-INSERT 的文件名又不能加引号，所以先复制到不含空格的 WORK
    src = WORK / f"salvage-{uuid.uuid4().hex[:12]}.dwg"
    shutil.copy2(drawing, src)
    dst.unlink(missing_ok=True)
    res = run_lisp('(setvar "FILEDIA" 0)',
                   post_lines=["_.-INSERT", f"*{src}", "0,0", "1", "0", "_.SAVEAS", "", f'"{lisp_path(dst)}"'],
                   timeout=timeout)
    src.unlink(missing_ok=True)
    if not (res.ok and dst.exists()):
        raise AcadError(f"救不回 {drawing}: {res.error}\n{res.log[-800:]}")
    return dst


def probe_xdata_apps(drawing: Path | str, timeout: int = DEFAULT_TIMEOUT) -> tuple[Result, dict[str, int]]:
    """列出图里真正挂了扩展数据的应用名及其对象数。

    市政图纸常把管网普查属性（管径、材质、埋深、井底高程…）以 XDATA 形式
    挂在普通图元上，一张图能注册几百个应用名，但多数没有实例。
    """
    drawing = _require(drawing)
    WORK.mkdir(parents=True, exist_ok=True)
    out = WORK / f"apps-{uuid.uuid4().hex[:12]}.txt"
    code = XDATA_PROBE_LISP.replace("%OUT%", lisp_str(lisp_path(out)))
    res = run_lisp(code, drawing=drawing, reads={"a": out}, timeout=timeout)
    out.unlink(missing_ok=True)

    apps: dict[str, int] = {}
    for line in res.outputs.get("a", "").splitlines():
        cols = line.rstrip("\n").split("\t")
        if len(cols) == 2 and cols[1].isdigit():
            apps[cols[0]] = int(cols[1])
    return res, dict(sorted(apps.items(), key=lambda kv: -kv[1]))


# 不同普查单位用的字段集不一样：有的管线挂"管径"，有的只有"起点埋深"；
# 有的井挂"井底高程"，有的只有"井底深"。单一锚点必漏，所以整组一起筛。
PIPE_ANCHORS = ["管径", "起点埋深", "终点埋深", "管道材质", "管线材料", "管线类型", "属性类型"]
WELL_ANCHORS = ["井底高程", "物探点号", "检查井井深", "检查井标识码", "附属物", "地面高程", "井底深", "井深"]
ALL_ANCHORS = PIPE_ANCHORS + WELL_ANCHORS


def extract_xdata(
    drawing: Path | str,
    anchor: str | list[str],
    types: str = "",
    max_entities: int = 5000,
    timeout: int = DEFAULT_TIMEOUT,
) -> tuple[Result, list[dict], int]:
    """取出挂了这些扩展数据应用的图元，连同它们的全部属性。

    anchor 可以是单个属性名，也可以是一组（逗号分隔或列表），多个锚点在
    AutoCAD 侧用 OR 一次筛完。types 限定实体类型（逗号分隔，支持通配符），
    留空则不限。导出的是每个对象的完整属性集，不只是锚点那一项。
    """
    drawing = _require(drawing)
    anchors = anchor.split(",") if isinstance(anchor, str) else list(anchor)
    anchors = [a.strip() for a in anchors if a.strip()]
    if not anchors:
        raise AcadError("至少要给一个锚点属性名")

    WORK.mkdir(parents=True, exist_ok=True)
    out = WORK / f"xd-{uuid.uuid4().hex[:12]}.txt"
    code = (
        XDATA_DUMP_LISP.replace("%OUT%", lisp_str(lisp_path(out)))
        .replace("%ANCHOR%", "(list " + " ".join(lisp_str(a) for a in anchors) + ")")
        .replace("%TYPES%", lisp_str(types.upper()))
        .replace("%MAXN%", str(int(max_entities)))
    )
    res = run_lisp(code, drawing=drawing, reads={"x": out}, timeout=timeout)
    out.unlink(missing_ok=True)

    matched = 0
    rows: list[dict] = []
    for line in res.outputs.get("x", "").splitlines():
        if line.startswith("#MATCHED="):
            matched = int(line.split("=", 1)[1] or 0)
            continue
        rec: dict[str, str] = {}
        for chunk in line.rstrip("\n").split("\t"):
            if "=" not in chunk:
                continue
            key, _, val = chunk.partition("=")
            rec[{"#": "句柄", "@": "类型", "~": "图层", "[": "起点", "]": "终点"}.get(key, key)] = val
        if rec:
            rows.append({k: v for k, v in rec.items() if v != ""})
    return res, rows, matched


def dump_drawing(
    drawing: Path | str,
    types: str = "",
    layers: str = "",
    max_entities: int = 20000,
    timeout: int = DEFAULT_TIMEOUT,
) -> tuple[Result, list[dict], int]:
    """导出图元明细。返回 (结果, 图元列表, 实际匹配总数)。

    max_entities 在 LISP 侧截断 —— 近 50 万图元的地形图全量导出会产生几百
    MB 中间文本，读进内存本身就是问题。
    """
    drawing = _require(drawing)
    WORK.mkdir(parents=True, exist_ok=True)
    out = WORK / f"dump-{uuid.uuid4().hex[:12]}.txt"
    code = (
        DUMP_LISP.replace("%OUT%", lisp_str(lisp_path(out)))
        .replace("%FILTER%", _ssget_filter(types, layers))
        .replace("%MAXN%", str(int(max_entities)))
    )
    res = run_lisp(code, drawing=drawing, reads={"dump": out}, timeout=timeout)
    out.unlink(missing_ok=True)

    text = res.outputs.get("dump", "")
    matched = 0
    body = []
    for line in text.splitlines():
        if line.startswith("#MATCHED="):
            matched = int(line.split("=", 1)[1] or 0)
        else:
            body.append(line)
    return res, parse_dump("\n".join(body)), matched


_RE_MTEXT_FONT = re.compile(r"\\[fF][^;]*;")
_RE_MTEXT_ARG = re.compile(r"\\[HWQACcpTX][^;\\]*;")
_RE_MTEXT_STACK = re.compile(r"\\S([^;]*);")
_RE_MTEXT_SWITCH = re.compile(r"\\[LlOoKkNn]")
_RE_PCT = re.compile(r"%%([dpcuoDPCUO%])")
_PCT_MAP = {"d": "°", "p": "±", "c": "Ø", "%": "%", "u": "", "o": ""}

# 同一对符号有两种写法：TEXT 用十进制 ASCII 码 %%146，MTEXT 用十六进制
# Unicode 转义 \U+0092（0x92=146、0x93=147）。同一份设计说明里两种都出现过。
_RE_CIRCLED = re.compile(r"%%146(\d{1,2})%%147|\\U\+0092(\d{1,2})\\U\+0093")
_RE_UNICODE = re.compile(r"\\U\+([0-9A-Fa-f]{4})")


def _circled(n: int) -> str:
    if 1 <= n <= 20:
        return chr(0x2460 + n - 1)
    return f"({n})"


def _sub_circled(m: re.Match) -> str:
    return _circled(int(m.group(1) or m.group(2)))


def _sub_unicode(m: re.Match) -> str:
    cp = int(m.group(1), 16)
    # 0x80-0x9F 是 C1 控制区，出现在这里一定是 shx 字体的自定义符号，
    # 转成控制字符只会变成不可见乱码，不如留着原样待人判读
    if 0x80 <= cp <= 0x9F:
        return m.group(0)
    return chr(cp)


def entity_text(ent: dict) -> str:
    """取 dump 出来的 TEXT/MTEXT 正文。

    MTEXT 正文超过 250 字符时，DXF 把它拆成若干条组码 3 续段 + 一条组码 1
    尾段（组码 1 在最后）。只读组码 1 会把长段落截成最后那一截 —— 设计说明、
    施工注意事项这类整段文字正是这样被砍掉开头的。
    """
    seg = ent.get("3")
    head = "".join(str(x) for x in seg) if isinstance(seg, list) else (str(seg) if seg else "")
    tail = ent.get("1")
    return head + (str(tail) if tail else "")


def clean_text(s: str, circled: bool = True) -> str:
    """剥掉 MTEXT 的格式码，还原人读的文字。

    %%nnn 的字形由图纸引用的 shx 字体决定，没有统一含义，所以默认只处理
    %%d/%%p/%%c 这些有标准语义的，其余原样保留。

    唯一的例外是 %%146N%%147 这个成对码，按带圈数字还原（circled=False 可
    关掉）。依据：这批图的 TSSD_Rein 样式里它只出现在地质分层表的"层号"列，
    数字序列 1,1,2,4,5 配合"素填土①-1;素填土①-2"的句式，且四个不同项目的
    结施图完全一致 —— 是岩土勘察的标准地层编号。转换结果会连同原文一起返回，
    便于核对。
    """
    if not isinstance(s, str) or not s:
        return s
    if circled:
        s = _RE_CIRCLED.sub(_sub_circled, s)
    s = _RE_UNICODE.sub(_sub_unicode, s)
    s = _RE_PCT.sub(lambda m: _PCT_MAP.get(m.group(1).lower(), m.group(0)), s)
    s = _RE_MTEXT_FONT.sub("", s)
    s = _RE_MTEXT_ARG.sub("", s)
    s = _RE_MTEXT_STACK.sub(lambda m: m.group(1).replace("^", "/"), s)
    s = _RE_MTEXT_SWITCH.sub("", s)
    s = s.replace("\\P", "\n").replace("\\~", " ")
    s = s.replace("{", "").replace("}", "")
    s = s.replace("\\\\", "\\")
    return s.strip()


def _is_string_code(code: int) -> bool:
    """DXF 规定这些组码区间存字符串，不能当数字解析。

    句柄（组码 5、330 等）是十六进制串，"200000" 数值化后就丢了它的本义。
    """
    return (
        0 <= code <= 9
        or 100 <= code <= 109
        or 300 <= code <= 369
        or 390 <= code <= 399
        or 410 <= code <= 419
        or 430 <= code <= 439
        or 470 <= code <= 481
    )


def _coerce(code: str, val: str):
    """把 dump 出来的字符串按组码还原成数值、坐标或原样字符串。

    l2s 给每个分量都补了逗号，所以 "10,20,0," 是点而 "25" 是标量。
    """
    try:
        c = int(code)
    except ValueError:
        return val
    if _is_string_code(c):
        return val
    if val.endswith(","):
        nums = []
        for part in val.rstrip(",").split(","):
            try:
                nums.append(float(part))
            except ValueError:
                return val
        return nums
    try:
        f = float(val)
        return int(f) if f.is_integer() and "." not in val else f
    except ValueError:
        return val


def parse_dump(text: str) -> list[dict]:
    """解析 dump 文本。

    同一组码可以重复出现（LWPOLYLINE 每个顶点都是组码 10），重复的一律
    收成列表，不能覆盖。
    """
    ents: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        rec: dict = {}
        for chunk in line.split("\t"):
            if "=" not in chunk:
                continue
            code, _, val = chunk.partition("=")
            code = code.strip()
            v = _coerce(code, val)
            if code in rec:
                if not isinstance(rec[code], list) or not isinstance(rec[code][0], (list, str)):
                    rec[code] = [rec[code]]
                rec[code].append(v)
            else:
                rec[code] = v
        if rec:
            rec.pop("-1", None)
            rec.pop("330", None)
            ents.append(rec)
    return ents


_ENT_BUILDERS: dict[str, callable] = {}


def _num(x) -> str:
    return f"{float(x):.8f}".rstrip("0").rstrip(".") or "0"


def _pt(code: int, p) -> str:
    vals = " ".join(_num(v) for v in p)
    return f"(list {code} {vals})"


HEADLESS_TYPES = (
    "LINE", "CIRCLE", "ARC", "TEXT", "LWPOLYLINE",
    "MTEXT", "SPLINE", "ELLIPSE", "HATCH", "INSERT",
    "ATTDEF", "ATTRIB", "LEADER", "MULTILEADER",
)
COM_ONLY_TYPES = ("DIMENSION",)

# 常用图纸尺寸。名字里是中文"毫米"不是 MM —— 写错会被当成未知命令，-PLOT
# 当场结束且不报错。用 `-PLOT` 的 `?` 可以列出设备支持的全部尺寸名。
PAPER_SIZES = {
    "A0": "ISO A0 (1189.00 x 841.00 毫米)",
    "A1": "ISO A1 (841.00 x 594.00 毫米)",
    "A2": "ISO A2 (594.00 x 420.00 毫米)",
    "A3": "ISO A3 (420.00 x 297.00 毫米)",
    "A4": "ISO A4 (297.00 x 210.00 毫米)",
}
DEFAULT_PLOTTER = "DWG To PDF.pc3"


def plot_lines(pdf: Path | str, layout: str = "", paper: str = "A2", **opts) -> list[str]:
    """拼 -PLOT 的命令行输入序列，一项一行，顺序不能动。

    17 项全部是必答的提示，少一项后面就整体错位、脚本挂死等输入。序列由
    accoreconsole 实测抓取（2026-09-22，中文版 2027）。

    走"否"分支（不详细配置）虽然只要 7 项，但打印区域会用默认的"显示"，
    实测出来是空白页 —— 所以这里固定走详细配置。
    """
    return [
        "-PLOT",
        "Y",                                          # 是否需要详细打印配置
        layout,                                       # 布局名（空=模型）
        str(opts.get("plotter", DEFAULT_PLOTTER)),    # 输出设备
        PAPER_SIZES.get(str(paper).upper(), str(paper)),
        str(opts.get("units", "M")),                  # 图纸单位 毫米
        str(opts.get("orientation", "L")),            # 横向
        "N",                                          # 不上下颠倒
        str(opts.get("area", "E")),                   # 打印区域 = 范围
        str(opts.get("scale", "F")),                  # 比例 = 布满
        str(opts.get("offset", "C")),                 # 居中
        "Y",                                          # 按样式打印
        str(opts.get("ctb", ".")),                    # 打印样式表（. = 无）
        "",                                           # 打印线宽 取默认
        "",                                           # 着色打印设置 取默认
        f'"{lisp_path(pdf)}"',
        "N",                                          # 不保存对页面设置的修改
        "Y",                                          # 继续打印
    ]


def _mtext_value(text: str) -> list[str]:
    """MTEXT 正文按 DXF 的 250 字符上限切段。

    组码 1 只能装最后一段，前面的段一律走组码 3，顺序不能颠倒 —— 这是 DXF
    格式规定，不是本项目的选择。读取侧 entity_text() 按同样规则拼回。
    """
    chunks = [text[i:i + 250] for i in range(0, len(text), 250)] or [""]
    parts = [f"(cons 3 {lisp_str(c)})" for c in chunks[:-1]]
    parts.append(f"(cons 1 {lisp_str(chunks[-1])})")
    return parts


def _clamped_knots(n: int, degree: int) -> list[float]:
    """n 个控制点、degree 次的钳位均匀节点矢量，长度必须正好是 n+degree+1。

    AutoCAD 不会替你补节点：组码 72 声明的数量对不上就直接拒收整条 SPLINE。
    """
    p = degree
    return [0.0] * (p + 1) + [float(i) for i in range(1, n - p)] + [float(n - p)] * (p + 1)


def _base_parts(kind: str, spec: dict) -> list[str]:
    parts = [f"(cons 0 {lisp_str(kind)})"]
    if spec.get("layer"):
        parts.append(f'(cons 8 {lisp_str(spec["layer"])})')
    return parts


def _simple_entmake(spec: dict) -> str:
    """把一个不需要额外命令配合的图元编译成单条 entmake。"""
    kind = str(spec.get("type", "")).upper()
    parts = _base_parts(kind, spec)

    if kind == "LINE":
        parts.append(_pt(10, spec["p1"]))
        parts.append(_pt(11, spec["p2"]))
    elif kind == "CIRCLE":
        parts.append(_pt(10, spec["center"]))
        parts.append(f'(cons 40 {_num(spec["radius"])})')
    elif kind == "ARC":
        parts.append(_pt(10, spec["center"]))
        parts.append(f'(cons 40 {_num(spec["radius"])})')
        parts.append(f'(cons 50 {_num(spec["start_angle"])})')
        parts.append(f'(cons 51 {_num(spec["end_angle"])})')
    elif kind == "TEXT":
        parts.append(_pt(10, spec["pos"]))
        parts.append(f'(cons 40 {_num(spec.get("height", 2.5))})')
        parts.append(f'(cons 1 {lisp_str(spec.get("value", ""))})')
        if spec.get("rotation"):
            parts.append(f'(cons 50 {_num(spec["rotation"])})')
    elif kind == "LWPOLYLINE":
        pts = spec["points"]
        parts = [
            f"(cons 0 {lisp_str(kind)})",
            '(cons 100 "AcDbEntity")',
        ] + ([f'(cons 8 {lisp_str(spec["layer"])})'] if spec.get("layer") else []) + [
            '(cons 100 "AcDbPolyline")',
            f"(cons 90 {len(pts)})",
            f'(cons 70 {1 if spec.get("closed") else 0})',
        ]
        parts.extend(_pt(10, p[:2]) for p in pts)
    elif kind == "MTEXT":
        parts = [
            f"(cons 0 {lisp_str(kind)})",
            '(cons 100 "AcDbEntity")',
        ] + ([f'(cons 8 {lisp_str(spec["layer"])})'] if spec.get("layer") else []) + [
            '(cons 100 "AcDbMText")',
            _pt(10, spec["pos"]),
            f'(cons 40 {_num(spec.get("height", 2.5))})',
            f'(cons 41 {_num(spec.get("width", 0))})',
            f'(cons 71 {int(spec.get("attachment", 1))})',
            f'(cons 7 {lisp_str(spec.get("style", "Standard"))})',
        ]
        parts.extend(_mtext_value(spec.get("value", "")))
        if spec.get("rotation"):
            parts.append(f'(cons 50 {_num(spec["rotation"])})')
    elif kind == "SPLINE":
        pts = [list(p) + [0.0] * (3 - len(p)) for p in spec["points"]]
        n = len(pts)
        degree = min(int(spec.get("degree", 3)), n - 1)
        if degree < 1:
            raise AcadError("SPLINE 至少要 2 个控制点")
        knots = _clamped_knots(n, degree)
        flags = 8 | (1 if spec.get("closed") else 0)
        parts = [
            f"(cons 0 {lisp_str(kind)})",
            '(cons 100 "AcDbEntity")',
        ] + ([f'(cons 8 {lisp_str(spec["layer"])})'] if spec.get("layer") else []) + [
            '(cons 100 "AcDbSpline")',
            f"(cons 70 {flags})",
            f"(cons 71 {degree})",
            f"(cons 72 {len(knots)})",
            f"(cons 73 {n})",
            "(cons 74 0)",
        ]
        parts.extend(f"(cons 40 {_num(k)})" for k in knots)
        parts.extend(_pt(10, p) for p in pts)
    elif kind == "ELLIPSE":
        parts = [
            f"(cons 0 {lisp_str(kind)})",
            '(cons 100 "AcDbEntity")',
        ] + ([f'(cons 8 {lisp_str(spec["layer"])})'] if spec.get("layer") else []) + [
            '(cons 100 "AcDbEllipse")',
            _pt(10, spec["center"]),
            _pt(11, spec["major_axis"]),
            f'(cons 40 {_num(spec.get("ratio", 1.0))})',
            f'(cons 41 {_num(spec.get("start_param", 0.0))})',
            f'(cons 42 {_num(spec.get("end_param", 6.28318531))})',
        ]
    elif kind == "INSERT":
        if spec.get("attribs_follow"):
            parts.append("(cons 66 1)")
        parts.append(f'(cons 2 {lisp_str(spec["block"])})')
        parts.append(_pt(10, spec["pos"]))
        sx, sy, sz = _scale3(spec.get("scale", 1))
        parts.append(f"(cons 41 {_num(sx)})")
        parts.append(f"(cons 42 {_num(sy)})")
        parts.append(f"(cons 43 {_num(sz)})")
        if spec.get("rotation"):
            parts.append(f'(cons 50 {_num(spec["rotation"])})')
    elif kind in ("ATTDEF", "ATTRIB"):
        parts.append(_pt(10, spec["pos"]))
        parts.append(f'(cons 40 {_num(spec.get("height", 2.5))})')
        parts.append(f'(cons 1 {lisp_str(spec.get("value", ""))})')
        if kind == "ATTDEF":
            parts.append(f'(cons 3 {lisp_str(spec.get("prompt", spec["tag"]))})')
        parts.append(f'(cons 2 {lisp_str(spec["tag"])})')
        parts.append(f'(cons 70 {int(spec.get("flags", 0))})')
        if spec.get("style"):
            parts.append(f'(cons 7 {lisp_str(spec["style"])})')
        if spec.get("rotation"):
            parts.append(f'(cons 50 {_num(spec["rotation"])})')
    elif kind == "LEADER":
        # 旧版 LEADER：几何完全由组码 10 的点列决定，比 MULTILEADER 可控。
        # MULTILEADER 的引线几何藏在 AcDbMLeader 的 context data 里，entmake
        # 给不全，AutoCAD 会用默认值补齐，位置就不是你指定的那个。
        pts = spec["points"]
        parts = [
            f"(cons 0 {lisp_str(kind)})",
            '(cons 100 "AcDbEntity")',
        ] + ([f'(cons 8 {lisp_str(spec["layer"])})'] if spec.get("layer") else []) + [
            '(cons 100 "AcDbLeader")',
            f'(cons 3 {lisp_str(spec.get("dimstyle", "Standard"))})',
            "(cons 71 1)",
            f'(cons 72 {1 if spec.get("spline") else 0})',
            "(cons 73 0)",
            f"(cons 76 {len(pts)})",
        ]
        parts.extend(_pt(10, p) for p in pts)
    elif kind == "MULTILEADER":
        parts = [
            f"(cons 0 {lisp_str(kind)})",
            '(cons 100 "AcDbEntity")',
        ] + ([f'(cons 8 {lisp_str(spec["layer"])})'] if spec.get("layer") else []) + [
            '(cons 100 "AcDbMLeader")',
            "(cons 270 2)",
            f'(cons 1 {lisp_str(spec.get("value", ""))})',
        ]
    else:
        raise AcadError(f"不支持的图元类型: {kind}")

    if spec.get("color"):
        parts.append(f'(cons 62 {int(spec["color"])})')
    if spec.get("ltype"):
        parts.append(f'(cons 6 {lisp_str(spec["ltype"])})')
    if spec.get("paper_space"):
        parts.append("(cons 67 1)")
    if kind == "TEXT" and spec.get("style"):
        parts.append(f'(cons 7 {lisp_str(spec["style"])})')
    return "(entmake (list " + " ".join(parts) + "))"


def _scale3(scale) -> tuple[float, float, float]:
    if isinstance(scale, (list, tuple)):
        vals = list(scale) + [1.0] * (3 - len(scale))
        return float(vals[0]), float(vals[1]), float(vals[2])
    return float(scale), float(scale), float(scale)


def _entity_exprs(spec: dict) -> list[str]:
    """把一个图元描述编译成一条或多条 LISP 表达式。

    HATCH 要先造边界再用 -HATCH 命令填充，INSERT 可能要先造块定义，所以
    单个描述会展开成多条表达式，不能沿用"一个图元一条 entmake"的形状。
    """
    kind = str(spec.get("type", "")).upper()

    if kind == "HATCH":
        boundary = dict(spec["boundary"])
        boundary.setdefault("layer", spec.get("layer"))
        pattern = spec.get("pattern", "SOLID").upper()
        # 用 (entlast) 抓刚造好的边界，比另开临时图层再 ssget 干净：不污染图层表，
        # 也不会误选到图上同名图层的既有图元。
        exprs = [
            _simple_entmake(boundary),
            "(setq _hb (ssadd (entlast)))",
        ]
        if pattern == "SOLID":
            exprs.append('(command "_.-HATCH" "_P" "SOLID" "_S" _hb "" "")')
        else:
            exprs.append(
                f'(command "_.-HATCH" "_P" {lisp_str(pattern)} '
                f'{_num(spec.get("scale", 1))} {_num(spec.get("angle", 0))} "_S" _hb "" "")'
            )
        if not spec.get("keep_boundary", True):
            exprs.append("(entdel (ssname _hb 0))")
        return exprs

    if kind == "INSERT" and (spec.get("define") or spec.get("attribs")):
        exprs: list[str] = []
        if spec.get("define"):
            # 块定义里有 ATTDEF 时标志位要置 2（"块带属性"），否则 AutoCAD
            # 接受块定义但插入后属性不跟随。
            has_attdef = any(
                str(s.get("type", "")).upper() == "ATTDEF" for s in spec["define"]
            )
            exprs.append(
                '(entmake (list (cons 0 "BLOCK") '
                f'(cons 2 {lisp_str(spec["block"])}) (cons 70 {2 if has_attdef else 0}) '
                f"{_pt(10, spec.get('base', [0, 0, 0]))}))"
            )
            exprs += [_simple_entmake(sub) for sub in spec["define"]]
            exprs.append('(entmake (list (cons 0 "ENDBLK")))')

        attribs = spec.get("attribs") or []
        ins = {k: v for k, v in spec.items() if k not in ("define", "attribs")}
        ins["attribs_follow"] = bool(attribs)
        exprs.append(_simple_entmake(ins))

        # ATTRIB 是 INSERT 的子实体：紧跟其后，再用 SEQEND 收尾，缺一个都读不出来
        base = list(spec["pos"]) + [0.0] * (3 - len(spec["pos"]))
        sx, sy, sz = _scale3(spec.get("scale", 1))
        for a in attribs:
            sub = dict(a)
            sub["type"] = "ATTRIB"
            sub.setdefault("layer", spec.get("layer"))
            # 组码 10 是 WCS 绝对坐标，不随块基点走。offset 是相对插入点的写法，
            # 按块缩放换算成绝对坐标；直接给 pos 就当绝对坐标用。
            off = sub.pop("offset", None)
            if off is not None:
                off = list(off) + [0.0] * (3 - len(off))
                sub["pos"] = [
                    base[0] + off[0] * sx,
                    base[1] + off[1] * sy,
                    base[2] + off[2] * sz,
                ]
            exprs.append(_simple_entmake(sub))
        if attribs:
            exprs.append('(entmake (list (cons 0 "SEQEND")))')
        return exprs

    return [_simple_entmake(spec)]


def _layer_expr(spec: dict) -> str:
    parts = [
        '(cons 0 "LAYER")',
        '(cons 100 "AcDbSymbolTableRecord")',
        '(cons 100 "AcDbLayerTableRecord")',
        f'(cons 2 {lisp_str(spec["name"])})',
        f'(cons 70 {int(spec.get("flags", 0))})',
        f'(cons 62 {int(spec.get("color", 7))})',
    ]
    if spec.get("ltype"):
        parts.append(f'(cons 6 {lisp_str(spec["ltype"])})')
    return "(entmake (list " + " ".join(parts) + "))"


def _ltype_expr(spec: dict) -> str:
    """线型。pattern 是一串划长：正数画线、负数留空、0 是点。"""
    dashes = [float(d) for d in spec.get("pattern", [0.5, -0.25])]
    parts = [
        '(cons 0 "LTYPE")',
        '(cons 100 "AcDbSymbolTableRecord")',
        '(cons 100 "AcDbLinetypeTableRecord")',
        f'(cons 2 {lisp_str(spec["name"])})',
        "(cons 70 0)",
        f'(cons 3 {lisp_str(spec.get("description", spec["name"]))})',
        "(cons 72 65)",
        f"(cons 73 {len(dashes)})",
        f'(cons 40 {_num(sum(abs(d) for d in dashes))})',
    ]
    for d in dashes:
        parts.append(f"(cons 49 {_num(d)})")
        parts.append("(cons 74 0)")
    return "(entmake (list " + " ".join(parts) + "))"


def _textstyle_expr(spec: dict) -> str:
    """文字样式。height=0 是"每次使用时询问"，中文图纸的常规做法。

    bigfont 是大字体文件，中文非它不可：font 只管 ASCII 字形，中文字形来自
    bigfont。本机缺 hztxt/tssdchn 等 5 个 shx，用系统自带的 gbcbig.shx 代替。
    """
    parts = [
        '(cons 0 "STYLE")',
        '(cons 100 "AcDbSymbolTableRecord")',
        '(cons 100 "AcDbTextStyleTableRecord")',
        f'(cons 2 {lisp_str(spec["name"])})',
        f'(cons 70 {int(spec.get("flags", 0))})',
        f'(cons 40 {_num(spec.get("height", 0.0))})',
        f'(cons 41 {_num(spec.get("width", 0.7))})',
        f'(cons 50 {_num(spec.get("oblique", 0.0))})',
        "(cons 71 0)",
        f'(cons 42 {_num(spec.get("last_height", 2.5))})',
        f'(cons 3 {lisp_str(spec.get("font", "gbenor.shx"))})',
        f'(cons 4 {lisp_str(spec.get("bigfont", "gbcbig.shx"))})',
    ]
    return "(entmake (list " + " ".join(parts) + "))"


def _dimstyle_expr(spec: dict) -> str:
    parts = [
        '(cons 0 "DIMSTYLE")',
        '(cons 100 "AcDbSymbolTableRecord")',
        '(cons 100 "AcDbDimStyleTableRecord")',
        f'(cons 2 {lisp_str(spec["name"])})',
        "(cons 70 0)",
        f'(cons 40 {_num(spec.get("scale", 1.0))})',
        f'(cons 140 {_num(spec.get("text_height", 3.5))})',
        f'(cons 141 {_num(spec.get("arrow_size", 2.5))})',
        f'(cons 147 {_num(spec.get("gap", 1.0))})',
        f'(cons 271 {int(spec.get("decimals", 0))})',
        "(cons 172 1)",
        "(cons 173 1)",
    ]
    if spec.get("text_style"):
        parts.append(f'(cons 340 {lisp_str(spec["text_style"])})')
    return "(entmake (list " + " ".join(parts) + "))"


STYLE_BUILDERS = {
    "LTYPE": _ltype_expr,
    "STYLE": _textstyle_expr,
    "TEXTSTYLE": _textstyle_expr,
    "DIMSTYLE": _dimstyle_expr,
}


def _style_expr(spec: dict) -> str:
    kind = str(spec.get("type", "")).upper()
    build = STYLE_BUILDERS.get(kind)
    if build is None:
        raise AcadError(
            f"不支持的样式类型: {kind}（可用 {'/'.join(sorted(set(STYLE_BUILDERS)))}）"
        )
    return build(spec)


def _layout_exprs(spec: dict) -> list[str]:
    """建图纸空间布局，并在其中开视口。

    LAYOUT/MVIEW 走 (command ...) 而不是 entmake —— 布局是带页面设置的字典
    对象，entmake 造不出来。这两个命令不读提示，所以 command 喂得进去。
    """
    name = spec["name"]
    exprs = [
        '(setvar "TILEMODE" 0)',
        f'(command "_.LAYOUT" "_N" {lisp_str(name)})',
        f'(setvar "CTAB" {lisp_str(name)})',
    ]
    for vp in spec.get("viewports", []):
        c1 = ",".join(str(float(v)) for v in vp["corner1"][:2])
        c2 = ",".join(str(float(v)) for v in vp["corner2"][:2])
        exprs.append(f'(command "_.MVIEW" {lisp_str(c1)} {lisp_str(c2)})')
    for e in spec.get("entities", []):
        sub = dict(e)
        sub["paper_space"] = True
        exprs += _entity_exprs(sub)
    if spec.get("entities") or spec.get("viewports"):
        exprs.append('(setvar "TILEMODE" 1)')
    return exprs


def draw(
    entities: list[dict],
    out_path: Path | str,
    layers: list[dict] | None = None,
    template: Path | str | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    styles: list[dict] | None = None,
    layouts: list[dict] | None = None,
    plot: dict | None = None,
) -> Result:
    """批量造图元并另存为 out_path，可选直接出 PDF。

    大部分图元走无头 accoreconsole。DIMENSION 无头做不到（命令返回成功但图中
    不会真的出现标注），所以先无头把其余图元画完存盘，再由 acad_com 打开这张
    图补标注。没有 DIMENSION 时完全不碰 COM。

    styles 是线型/文字样式/标注样式，要在引用它们的图元之前建；layouts 是图纸
    空间布局和视口；plot 给了就在存盘后导出 PDF，形如
    {"pdf": 路径, "paper": "A2", "layout": "", "area": "E"}。
    """
    # accoreconsole 的启动目录是 FONTS，脚本里的相对路径会落到那里
    out_path = Path(out_path).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        out_path.unlink()

    headless = [e for e in entities if str(e.get("type", "")).upper() not in COM_ONLY_TYPES]
    com_only = [e for e in entities if str(e.get("type", "")).upper() in COM_ONLY_TYPES]

    lines: list[str] = [
        # FILEDIA=0 是必须的：否则 SAVEAS 会弹文件对话框，无头脚本当场挂死
        '(setvar "FILEDIA" 0)',
        '(setvar "CMDECHO" 0)',
    ]
    # 顺序有意义：样式表要先于引用它的图层，图层要先于图元
    lines += [_style_expr(s) for s in (styles or [])]
    lines += [_layer_expr(l) for l in (layers or [])]
    for e in headless:
        lines += _entity_exprs(e)
    for lo in (layouts or []):
        lines += _layout_exprs(lo)

    # SAVEAS 走 .scr 原生命令行：一行一次输入，先空行接受默认格式再给路径。
    # 用 (command "_.SAVEAS" "" path) 会吃不到提示并把脚本挂死。
    post = ["_.SAVEAS", "", f'"{lisp_path(out_path)}"']

    pdf = None
    if plot:
        pdf = Path(plot["pdf"]).resolve()
        pdf.parent.mkdir(parents=True, exist_ok=True)
        if pdf.exists():
            pdf.unlink()
        opts = {k: v for k, v in plot.items() if k not in ("pdf", "layout", "paper")}
        post += plot_lines(
            pdf, layout=plot.get("layout", ""), paper=plot.get("paper", "A2"), **opts
        )

    res = run_lisp("\n".join(lines), drawing=template, post_lines=post, timeout=timeout)
    if res.ok and not out_path.exists():
        res.ok = False
        res.error = res.error or f"脚本跑完但没生成 {out_path}"
    if pdf is not None:
        if pdf.exists():
            res.outputs["pdf"] = f"{pdf} ({pdf.stat().st_size} 字节)"
        elif res.ok:
            res.ok = False
            res.error = f"图已存盘但没出 PDF：{pdf}"
    if not res.ok or not com_only:
        return res

    try:
        import acad_com
    except ImportError as exc:
        res.ok = False
        res.error = f"{len(com_only)} 个 DIMENSION 需要 COM 通道，但 {exc}"
        return res

    try:
        info = acad_com.add_dimensions(out_path, com_only, timeout=max(timeout, 300))
        res.outputs["com"] = (
            f"标注 {info['added']} 个，{info['elapsed']}s: {', '.join(info['types'])}"
        )
    except Exception as exc:
        res.ok = False
        res.error = f"无头部分已存盘，但补标注失败: {exc}"
    return res


if __name__ == "__main__":
    import json
    import sys

    target = sys.argv[1] if len(sys.argv) > 1 else str(
        Path(os.environ["USERPROFILE"]) / ".autocad-mcp/cap-test.dwg"
    )
    res, ents, matched = dump_drawing(target)
    print(f"ok={res.ok} elapsed={res.elapsed}s 图元数={len(ents)}/{matched}")
    if res.error:
        print(f"error: {res.error}")
    print(json.dumps(ents, ensure_ascii=False, indent=2)[:2500])
    if not res.ok and res.log:
        print("--- journal ---")
        print(res.log[-1200:])
