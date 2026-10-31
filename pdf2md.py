#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pdf2md.py —— 用 MiniCPM-OCR + llama-mtmd-cli 把 PDF 逐页转成 Markdown。

底层调用:
    llama-mtmd-cli.exe -m <model> --mmproj <mmproj> --image <png> -p "<prompt>"

依赖: PyMuPDF (pip install pymupdf)。llama-mtmd-cli 与 gguf 模型自备。

常用:
    python pdf2md.py in.pdf                      # 自动发现模型，多则交互选择
    python pdf2md.py in.pdf -m MiniCPM-OCR-1.0-Q8_0.gguf \\
                            --mmproj mmproj-MiniCPM-OCR-1.0-Q8_0.gguf
    python pdf2md.py in.pdf -o out.md --pages 1-10 --dpi 200 --jobs 2
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

try:
    import pymupdf
except ImportError:  # pragma: no cover
    sys.exit("缺少依赖 PyMuPDF，请先执行: pip install pymupdf")

# Windows 下 stderr 重定向到文件时默认走 GBK，中文/方块字符会炸，统一成 UTF-8
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

DEFAULT_PROMPT = (
    "OCR this page. Transcribe every character you see verbatim, preserving the "
    "original layout and reading order. Never answer, solve, or explain any "
    "problem. Output all mathematical formulas in LaTeX format."
)
EXE_NAME = "llama-mtmd-cli.exe" if os.name == "nt" else "llama-mtmd-cli"
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# 打包成 exe 之后 __file__ 指向 PyInstaller 的临时解压目录，
# 得改用 exe 自身所在目录，否则模型和 llama.cpp 都找不到。
if getattr(sys, "frozen", False):
    SCRIPT_DIR = Path(sys.executable).resolve().parent
else:
    SCRIPT_DIR = Path(__file__).resolve().parent

_print_lock = threading.Lock()


def log(msg: str, quiet: bool = False) -> None:
    if quiet:
        return
    with _print_lock:
        print(msg, file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# 进度显示：进度条 + 速度 + 预计剩余时间
# ---------------------------------------------------------------------------

# 中文 Windows 控制台可能不支持方块字符，做个降级
try:
    "█".encode(sys.stderr.encoding or "utf-8")
    _BAR_FULL, _BAR_EMPTY = "█", "░"
except (UnicodeEncodeError, LookupError):
    _BAR_FULL, _BAR_EMPTY = "#", "-"


def _fmt_dur(sec: float) -> str:
    """秒 -> 1m02s / 1h02m 之类的紧凑写法。"""
    sec = max(0, int(round(sec)))
    if sec < 60:
        return f"{sec}s"
    m, s = divmod(sec, 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m"


def _fmt_bar(frac: float, width: int = 20) -> str:
    frac = min(1.0, max(0.0, frac))
    filled = int(round(frac * width))
    return _BAR_FULL * filled + _BAR_EMPTY * (width - filled)


class Progress:
    """线程安全的进度显示。

    - TTY：单行原地刷新，不刷屏
    - 非 TTY（重定向/管道）：每页一行，避免 \\r 把日志搅烂
    """

    def __init__(self, total: int, enabled: bool = True, stream=None) -> None:
        self.total = max(1, total)
        self.enabled = enabled
        self.stream = stream or sys.stderr
        try:
            self.is_tty = bool(self.stream.isatty())
        except (AttributeError, ValueError):
            self.is_tty = False
        self.done = 0
        self.ok = 0
        self.blank = 0
        self.failed = 0
        self.chars = 0
        self.t0 = time.time()
        self._lock = threading.Lock()
        self._prev_len = 0

    @property
    def elapsed(self) -> float:
        return time.time() - self.t0

    def _stats(self) -> tuple[float, float, float]:
        """返回 (平均每页秒数, 剩余秒数, 完成比例)。"""
        frac = self.done / self.total
        if self.done == 0:
            return 0.0, 0.0, 0.0
        avg = self.elapsed / self.done
        return avg, avg * (self.total - self.done), frac

    def page_done(self, page: int, status: str, secs: float, chars: int = 0) -> None:
        if not self.enabled:
            return
        with self._lock:
            self.done += 1
            self.chars += chars
            if status.startswith("✓"):
                self.ok += 1
            elif status.startswith("·"):
                self.blank += 1
            else:
                self.failed += 1

            avg, remain, frac = self._stats()
            eta = datetime.fromtimestamp(time.time() + remain).strftime("%H:%M:%S")
            line = (
                f"[{_fmt_bar(frac, 20)}] {frac * 100:3.0f}% "
                f"{self.done}/{self.total} | {avg:4.1f}s/页 | "
                f"剩余 {_fmt_dur(remain)} (约 {eta}) | p{page} {status} {secs:.1f}s"
            )

            if self.is_tty:
                pad = max(0, self._prev_len - len(line))
                self.stream.write("\r" + line + " " * pad)
                self.stream.flush()
                self._prev_len = len(line)
            else:
                self.stream.write(line + "\n")
                self.stream.flush()

    def note(self, text: str) -> None:
        """在进度行之上插一条说明（不破坏 TTY 单行状态）。"""
        if not self.enabled:
            return
        with self._lock:
            if self.is_tty and self._prev_len:
                self.stream.write("\r" + " " * self._prev_len + "\r")
            self.stream.write(text + "\n")
            self.stream.flush()
            self._prev_len = 0

    def close(self) -> None:
        if not self.enabled:
            return
        with self._lock:
            if self.is_tty and self._prev_len:
                self.stream.write("\n")
                self.stream.flush()
                self._prev_len = 0


# ---------------------------------------------------------------------------
# 定位可执行文件与模型
# ---------------------------------------------------------------------------


def find_cli(explicit: str | None = None) -> Path:
    """按优先级找 llama-mtmd-cli：显式路径 > 脚本目录 > PATH。"""
    if explicit:
        p = Path(explicit).expanduser().resolve()
        if p.is_dir():
            p = p / EXE_NAME
        if not p.is_file():
            sys.exit(f"找不到可执行文件: {p}")
        return p

    candidates: list[Path] = []
    # 同级目录里的 llama-* 发行包
    for d in sorted(SCRIPT_DIR.glob("llama-*")):
        if d.is_dir():
            candidates.append(d / EXE_NAME)
    candidates.append(SCRIPT_DIR / EXE_NAME)

    for c in candidates:
        if c.is_file():
            return c.resolve()

    found = shutil.which(EXE_NAME.replace(".exe", ""))
    if found:
        return Path(found)

    sys.exit(
        f"找不到 {EXE_NAME}。\n"
        f"  已查找: {SCRIPT_DIR} 及其下的 llama-* 目录、以及 PATH。\n"
        f"  可用 --bin 显式指定，例如 --bin D:/llama/llama-mtmd-cli.exe"
    )


def _is_mmproj(p: Path) -> bool:
    return "mmproj" in p.name.lower()


def _pair_key(p: Path) -> str:
    """模型与投影器的配对键：去掉 mmproj- 前缀和 .gguf 后缀，小写。"""
    stem = p.name
    if stem.lower().endswith(".gguf"):
        stem = stem[:-5]
    stem = re.sub(r"^mmproj[-_.]", "", stem, flags=re.I)
    return stem.lower()


@dataclass
class ModelPair:
    model: Path
    mmproj: Path | None

    def label(self) -> str:
        name = self.model.name
        if self.mmproj:
            return f"{name}   [mmproj: {self.mmproj.name}]"
        return f"{name}   [⚠ 未找到匹配的 mmproj]"


def discover_models(roots: list[Path]) -> list[ModelPair]:
    """扫描目录下的 gguf，按文件名把模型和 mmproj 配对。"""
    models: dict[Path, list[Path]] = {}
    mmprojs: dict[Path, list[Path]] = {}
    seen: set[Path] = set()

    for root in roots:
        if not root or not root.is_dir():
            continue
        for p in sorted(root.glob("*.gguf")):
            rp = p.resolve()
            if rp in seen:
                continue
            seen.add(rp)
            bucket = mmprojs if _is_mmproj(p) else models
            bucket.setdefault(rp.parent, []).append(rp)

    pairs: list[ModelPair] = []
    all_mm = [m for lst in mmprojs.values() for m in lst]
    for parent, lst in models.items():
        local_mm = mmprojs.get(parent, [])
        for m in lst:
            key = _pair_key(m)
            match = next((x for x in local_mm if _pair_key(x) == key), None)
            if match is None and len(local_mm) == 1:
                match = local_mm[0]
            if match is None and len(all_mm) == 1:
                match = all_mm[0]
            pairs.append(ModelPair(m, match))
    return pairs


def finite_choice(pairs: list[ModelPair], non_interactive: bool) -> ModelPair:
    """多个候选时交互选择；非交互场景直接报错列出。"""
    if not pairs:
        sys.exit(
            "在搜索路径下没发现 .gguf 模型。\n"
            "  请用 -m/--model 指定模型，并用 --mmproj 指定投影器。"
        )
    if len(pairs) == 1:
        return pairs[0]

    if non_interactive or not sys.stdin.isatty():
        lines = "\n".join(f"  [{i}] {p.label()}" for i, p in enumerate(pairs, 1))
        sys.exit(f"发现多个模型，请用 -m 指定其中一个：\n{lines}")

    print("发现多个模型，请选择：", file=sys.stderr)
    for i, p in enumerate(pairs, 1):
        print(f"  [{i}] {p.label()}", file=sys.stderr)
    while True:
        try:
            raw = input(f"序号 [1-{len(pairs)}] (回车=1): ").strip()
        except (EOFError, KeyboardInterrupt):
            sys.exit("\n已取消。")
        if not raw:
            return pairs[0]
        if raw.isdigit() and 1 <= int(raw) <= len(pairs):
            return pairs[int(raw) - 1]
        print("输入无效，请重试。", file=sys.stderr)


def resolve_model(args: argparse.Namespace) -> ModelPair:
    """模型/投影器解析：命令行 > 目录扫描 > 交互选择。"""
    model = args.model
    mmproj = args.mmproj

    if model:
        mp = Path(model).expanduser()
        if mp.is_dir():
            pairs = discover_models([mp.resolve()])
            pair = finite_choice(pairs, args.non_interactive)
            return ModelPair(pair.model, Path(mmproj).expanduser().resolve() if mmproj else pair.mmproj)
        mp = mp.resolve()
        if not mp.is_file():
            sys.exit(f"模型文件不存在: {mp}")
        if mmproj:
            mm = Path(mmproj).expanduser().resolve()
            if not mm.is_file():
                sys.exit(f"mmproj 文件不存在: {mm}")
            return ModelPair(mp, mm)
        # 未给 mmproj：在同目录里按名字配对
        pairs = discover_models([mp.parent])
        hit = next((p for p in pairs if p.model == mp), None)
        return hit or ModelPair(mp, None)

    roots = [Path(d).expanduser().resolve() for d in args.model_dir] if args.model_dir else []
    roots += [SCRIPT_DIR, Path.cwd()]
    return finite_choice(discover_models(roots), args.non_interactive)


# ---------------------------------------------------------------------------
# 页码解析
# ---------------------------------------------------------------------------


def parse_pages(spec: str | None, total: int) -> list[int]:
    """'1-3,5,8-' -> [1,2,3,5,8,...]，1-based。空 = 全部。"""
    if not spec:
        return list(range(1, total + 1))
    out: list[int] = []
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            a, _, b = chunk.partition("-")
            start = int(a) if a.strip() else 1
            end = int(b) if b.strip() else total
            out.extend(range(start, end + 1))
        else:
            out.append(int(chunk))
    picked = sorted({p for p in out if 1 <= p <= total})
    if not picked:
        sys.exit(f"页码范围 {spec} 与文档页数 {total} 不匹配")
    return picked


# ---------------------------------------------------------------------------
# 输出清洗
# ---------------------------------------------------------------------------

_RE_SEP = re.compile(r"^\s*-{3,}\s*$")
_RE_FENCE = re.compile(r"^\s*```[\w+-]*\s*$")
_RE_CHATTER = re.compile(
    r"^(of course|certainly|sure|here('s| is)|below is|i'll|i will)\b"
    r"|^(好的|以下是|下面是|我将|为您)|transcri",
    re.I,
)
# \text{...} / \mathrm{...} / \textbf{...} 纯文本包装
_RE_TEXTTAG = re.compile(r"\\(?:text|textrm|textnormal|mathrm|mathbf|textbf|textit|operatorname)\s*\{([^{}]*)\}")
# 强数学结构：出现就认定是真公式，绝不拆
_RE_STRONGMATH = re.compile(
    r"\\(?:frac|dfrac|tfrac|sqrt|sum|prod|int|iint|oint|lim|begin|end|left|right"
    r"|matrix|pmatrix|bmatrix|vmatrix|cases|binom|overline|underline|vec|hat|bar"
    r"|partial|nabla|infty|alpha|beta|gamma|delta|epsilon|theta|lambda|mu|pi"
    r"|sigma|phi|omega|Omega|times|cdot|div|pm|leq|geq|neq|approx)\b"
)
_RE_TEXTTAG = re.compile(
    r"\\(?:text|textrm|textnormal|textbf|textit|operatorname|mathrm|mathbf|mathsf|mathtt)"
    r"\s*\{([^{}]*)\}"
)


_RE_BLOCKMATH = re.compile(r"\\\[(.*?)\\\]|\$\$(.*?)\$\$", re.S)# 模型偶尔吐出的无意义起头词
_RE_JUNK_HEAD = re.compile(r"^\s*(?:Nolimit|No limit|n/a)\s*$\n?", re.I | re.M)


def _unwrap_block(inner: str) -> str | None:
    """如果一个「公式块」其实主要装的是 \\text{正文}，返回展开后的纯文本。"""
    if _RE_STRONGMATH.search(inner):
        return None  # 真公式，别动

    texted = sum(len(x) for x in _RE_TEXTTAG.findall(inner))
    shell = _RE_TEXTTAG.sub("", inner)
    shell = re.sub(r"\\[\[\]]", "", shell)
    visible_shell = len(re.sub(r"\s", "", shell))
    if texted == 0 or texted / max(1, texted + visible_shell) < 0.5:
        return None  # 主要是公式，保留

    out = _RE_TEXTTAG.sub(r"\1", inner)
    out = re.sub(r"\\\[|\\\]", "\n", out)
    # 脱壳后残留的裸变量（u_{11}、U_{D}）补回行内公式标记
    out = re.sub(r"(?<![\w$\\)])([A-Za-z])([_^]\{[^{}]*\})+(?![\w$])",
                 lambda m: "$" + m.group(0) + "$", out)
    out = re.sub(r"\n{2,}", "\n", out)
    return out.strip()


def _unwrap_pseudomath(text: str) -> str:
    """把「伪公式块」还原成普通文本。

    模型的惯用毛病：整段中文正文被包进 \\[ \\text{...} \\]。
    判据不是「有没有数学命令」（正文里的 u_{11} 也算），而是
    \\text{} 里装了多少可见字符 —— 装了大半就是它在把正文当公式。
    """
    def repl(m: re.Match) -> str:
        inner = m.group(1) if m.group(1) is not None else (m.group(2) or "")
        fixed = _unwrap_block(inner)
        if fixed is None:
            return m.group(0)
        return "\n" + fixed + "\n"

    return _RE_BLOCKMATH.sub(repl, text)


def _strip_outer_fence(lines: list[str]) -> list[str]:
    """去掉整体包裹的 ``` 代码围栏。"""
    idx = [i for i, ln in enumerate(lines) if ln.strip()]
    if len(idx) >= 2 and _RE_FENCE.match(lines[idx[0]]) and _RE_FENCE.match(lines[idx[-1]]):
        return lines[idx[0] + 1: idx[-1]]
    return lines


def _strip_preamble(lines: list[str]) -> list[str]:
    """去掉模型爱加的寒暄段：'Of course, I'll transcribe...' + 紧跟的分隔线。"""
    idx = [i for i, ln in enumerate(lines) if ln.strip()]
    if not idx:
        return lines

    start = idx[0]
    end = start
    while end + 1 < len(lines) and lines[end + 1].strip() and not _RE_SEP.match(lines[end + 1]):
        end += 1

    para = "\n".join(lines[start: end + 1]).strip()
    nxt_is_sep = end + 1 < len(lines) and _RE_SEP.match(lines[end + 1])

    # 只有「明确是寒暄」才删，避免误伤正文首段
    if _RE_CHATTER.search(para) and len(para) <= 400:
        cut = end + 1
        if nxt_is_sep:
            cut += 1
        while cut < len(lines) and not lines[cut].strip():
            cut += 1
        return lines[cut:]
    return lines


def _strip_edge_separators(lines: list[str]) -> list[str]:
    """去掉首尾孤立的分隔线。"""
    out = list(lines)
    while out and (not out[0].strip() or _RE_SEP.match(out[0])):
        out.pop(0)
    while out and (not out[-1].strip() or _RE_SEP.match(out[-1])):
        out.pop()
    return out


def _normalize_math(text: str) -> str:
    """\\( ... \\) -> $...$ ；\\[ ... \\] -> $$...$$（markdown 友好）。"""
    text = re.sub(r"\\\((.+?)\\\)", lambda m: "$" + m.group(1).strip() + "$", text, flags=re.S)
    text = re.sub(
        r"\\\[(.+?)\\\]",
        lambda m: "\n$$\n" + m.group(1).strip() + "\n$$\n",
        text,
        flags=re.S,
    )
    return text


_RE_OTSL_BLOCK = re.compile(r"<otsl>(.*?)</otsl>", re.S)
_RE_OTSL_TAG = re.compile(r"<(?P<tag>fcel|ecel|lcel|ucel|ched|rhed|nl)\s*/?>")


def _unwrap_otsl(text: str) -> str:
    """把模型输出的 OTSL 表格标记还原成普通文本。

    MiniCPM-OCR 遇到版式规整的页面会套 <otsl><fcel>...<nl>...</otsl>，
    这是给表格结构用的。单列页面直接摊平成行；多列时按 | 分隔。
    """
    if "<otsl>" not in text:
        return text

    def conv(m: re.Match) -> str:
        body = m.group(1)
        rows: list[list[str]] = []
        cur: list[str] = []
        pos = 0
        for tag in _RE_OTSL_TAG.finditer(body):
            chunk = body[pos:tag.start()].strip()
            pos = tag.end()
            name = tag.group("tag")
            if name == "nl":
                if chunk:
                    cur.append(chunk)
                if any(c.strip() for c in cur):
                    rows.append(cur)
                cur = []
                continue
            if chunk:
                cur.append(chunk)
            # 空单元格标签不产生内容
        if chunk_rest := body[pos:].strip():
            cur.append(chunk_rest)
        if any(c.strip() for c in cur):
            rows.append(cur)

        lines = []
        for r in rows:
            cells = [c for c in r if c.strip()]
            if not cells:
                continue
            lines.append(cells[0] if len(cells) == 1 else " | ".join(cells))
        return "\n\n".join(lines)

    return _RE_OTSL_BLOCK.sub(conv, text)


def clean_output(raw: str) -> str:
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    # 清掉推理模型的 think 块与 chat 模板残留
    # 模型偶尔把 <think> 拆成 "<" + 换行 + "think"，所以闭合尖括号设为可选
    text = re.sub(r"[<\[]\s*\\?/?\s*(?:t)?hink\b\s*[>\]]?", "", text, flags=re.I)
    text = re.sub(r"</?\|?im_(?:start|end)\|?>", "", text)
    text = re.sub(r"^\s*\\?/?\s*think\s*$\n?", "", text, flags=re.I | re.M)
    text = re.sub(r"(?m)^[ \t]*[<>\]\[]+[ \t]*$\n?", "", text)
    text = _RE_JUNK_HEAD.sub("", text)
    text = text.strip("\n")
    if not text.strip():
        return ""
    text = _unwrap_pseudomath(text)
    text = _unwrap_otsl(text)
    lines = text.split("\n")
    lines = _strip_outer_fence(lines)
    lines = _strip_preamble(lines)
    lines = _strip_edge_separators(lines)
    out = "\n".join(lines)
    out = _normalize_math(out)
    out = re.sub(r"[ \t]+\n", "\n", out)
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r"([\u4e00-\u9fff])\s+([，。、；：？！）》」』])", r"\1\2", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


# ---------------------------------------------------------------------------
# 单页 OCR
# ---------------------------------------------------------------------------


def looks_blank(pix) -> bool:
    """抽样统计暗像素，判断是否空白页（省掉一次 2-6 秒推理）。"""
    samples = pix.samples
    if not samples:
        return True
    step = max(3, (len(samples) // 120_000) * 3)
    total = dark = 0
    for i in range(0, len(samples) - 2, step):
        total += 1
        if samples[i] < 200:
            dark += 1
    return total > 0 and dark / total < 0.0008


@dataclass
class PageResult:
    page: int
    text: str = ""
    seconds: float = 0.0
    error: str | None = None
    blank: bool = False
    retried: int = 0


# 退化输出：模型偶发只吐 think/代码围栏而不给正文
_RE_DEGENERATE = re.compile(r"^[\s`<>\|/]*(?:think)?[\s`<>\|/]*$", re.I)


def is_degenerate(text: str, blank_hint: bool = False) -> bool:
    """判断这页是不是「什么都没识别出来」。"""
    core = re.sub(r"</?think>|<\|.*?\|>", "", text, flags=re.I).strip()
    core = core.strip("`~ \t\r\n")
    if not core:
        return True
    if len(core) < 12 and not re.search(r"[\u4e00-\u9fff\w]", core):
        return True
    return False


def _rotate_samples(samples: bytes, w: int, h: int, n: int, deg: int) -> tuple[bytes, int, int]:
    """把像素缓冲按 90 的倍数旋转（无插值，纯重排）。"""
    try:
        import numpy as np
    except ImportError:
        raise RuntimeError("需要 numpy 才能旋转扫描原图")
    arr = np.frombuffer(samples, dtype=np.uint8)
    if n == 1:
        arr = arr.reshape(h, w)
    else:
        arr = arr.reshape(h, w, n)
    # PDF /Rotate 是顺时针角度；numpy rot90 逆时针，故取 k = -(deg/90)
    k = (-(deg // 90)) % 4
    out = np.rot90(arr, k)
    out = np.ascontiguousarray(out)
    nh, nw = out.shape[:2]
    return out.tobytes(), nw, nh


def _downscale_samples(data: bytes, w: int, h: int, n: int, max_pixels: float) -> tuple[bytes, int, int]:
    """按像素上限等比缩小（PIL 不可用时抛错，由调用方退回渲染）。"""
    from PIL import Image  # 可选依赖，缺失就走渲染路径

    img = Image.frombytes("L" if n == 1 else ("RGB" if n == 3 else "RGBA"), (w, h), data)
    ratio = (max_pixels / (w * h)) ** 0.5
    nw, nh = max(1, int(w * ratio)), max(1, int(h * ratio))
    img = img.resize((nw, nh), Image.LANCZOS)
    return img.tobytes(), nw, nh


def extract_page_image(
    doc, page_no: int, dpi: int, max_pixels: float, use_original: bool
) -> tuple[bytes, int, int, int, str]:
    """取一页的位图，并按像素预算控制尺寸。

    为什么不用固定 dpi：这个模型对像素总量很敏感，超过约 1.6~2 MP 后
    会退化成空输出（实测 200dpi 稳定失败、150dpi 稳定成功）。
    所以按 max_pixels 反推有效 dpi，比盲目调 dpi 稳。

    use_original=True 时优先抽扫描原图，省掉一次重采样。

    返回 (样本字节, 宽, 高, 通道数, 来源说明)。
    """
    page = doc[page_no - 1]
    area_pt = max(1.0, page.rect.width * page.rect.height)

    # 像素预算对应的 dpi 上限
    budget_dpi = ((max_pixels * 72.0 * 72.0) / area_pt) ** 0.5
    eff_dpi = max(40, int(min(dpi, budget_dpi)))

    if use_original:
        try:
            infos = page.get_image_info(xrefs=True)
            big = [
                i for i in infos
                if i.get("xref") and abs(pymupdf.Rect(i["bbox"])) > abs(page.rect) * 0.92
            ]
            if len(big) == 1:
                xref = big[0]["xref"]
                pix = pymupdf.Pixmap(doc, xref)
                if pix.n > 4:
                    pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
                data, w, h, n = pix.samples, pix.width, pix.height, pix.n
                rot = (page.rotation or 0) % 360
                if rot:
                    data, w, h = _rotate_samples(data, w, h, n, rot)
                src = f"原图 xref={xref}"
                if rot:
                    src += f" rot={rot}"
                if w * h > max_pixels:
                    try:
                        data, w, h = _downscale_samples(data, w, h, n, max_pixels)
                        src += f" →{w}x{h}"
                    except Exception:  # noqa: BLE001
                        raise  # 缩不了就退回渲染
                return data, w, h, n, src
        except Exception:  # noqa: BLE001
            pass  # 抽原图失败不算错，渲染兜底

    pix = page.get_pixmap(dpi=eff_dpi)
    return pix.samples, pix.width, pix.height, pix.n, f"渲染 {eff_dpi}dpi"


def _samples_to_png(data: bytes, w: int, h: int, n: int, path: Path) -> None:
    cs = {1: pymupdf.csGRAY, 3: pymupdf.csRGB, 4: pymupdf.csCMYK}.get(n, pymupdf.csGRAY)
    pymupdf.Pixmap(cs, w, h, data, False).save(str(path))


def ocr_page(
    *,
    cli: Path,
    model: Path,
    mmproj: Path | None,
    doc_path: Path,
    page_no: int,
    dpi: int,
    max_pixels: float,
    use_original: bool,
    prompt: str,
    threads: int | None,
    ctx: int | None,
    temp: float,
    timeout: float,
    image_out: Path | None,
    workdir: Path,
    clean: bool,
    retries: int = 2,
) -> PageResult:
    """取图 -> 调 llama-mtmd-cli -> 清洗；输出退化时自动降像素重试。"""
    t0 = time.time()
    png = image_out or (workdir / f"page-{page_no:04d}.png")

    # 每轮重试把像素预算打八折：模型在大图上会「拒绝作答」，缩小常能救回
    last_text = ""
    last_src = ""
    attempts = 0
    try:
        doc = pymupdf.open(doc_path)
        try:
            if page_no > doc.page_count:
                return PageResult(page_no, error=f"页号超出范围（共 {doc.page_count} 页）")

            cur_pixels = max_pixels
            for attempt in range(retries + 1):
                attempts = attempt + 1
                data, w, h, n, src = extract_page_image(
                    doc, page_no, dpi, cur_pixels, use_original
                )
                last_src = src
                _samples_to_png(data, w, h, n, png)

                if looks_blank(pymupdf.Pixmap(pymupdf.csGRAY if n == 1 else pymupdf.csRGB, w, h, data, False)):
                    return PageResult(
                        page_no, text="", blank=True,
                        seconds=time.time() - t0, retried=attempt,
                    )

                raw = _run_cli(
                    cli, model, mmproj, png, prompt, temp, threads, ctx, timeout
                )
                if isinstance(raw, PageResult):
                    raw.page = page_no
                    raw.seconds = time.time() - t0
                    return raw

                text = clean_output(raw) if clean else raw.strip()
                last_text = text
                if not is_degenerate(text):
                    return PageResult(
                        page_no, text=text, seconds=time.time() - t0, retried=attempt
                    )
                # 退化了：缩像素再来一次
                cur_pixels = max(200_000.0, cur_pixels * 0.8)
        finally:
            doc.close()
    except Exception as exc:  # noqa: BLE001
        return PageResult(page_no, error=f"处理失败: {exc}", seconds=time.time() - t0)
    finally:
        if not image_out:
            png.unlink(missing_ok=True)

    if not last_text.strip():
        return PageResult(
            page_no,
            error=f"模型连续 {attempts} 次无输出（最后一轮 {last_src}）",
            seconds=time.time() - t0,
            retried=attempts - 1,
        )
    return PageResult(
        page_no, text=last_text, seconds=time.time() - t0, retried=attempts - 1
    )


def _run_cli(
    cli: Path, model: Path, mmproj: Path | None, png: Path,
    prompt: str, temp: float, threads: int | None, ctx: int | None, timeout: float,
) -> str | PageResult:
    """跑一次 llama-mtmd-cli，成功返回 stdout，失败返回带 error 的 PageResult。"""
    cmd = [
        str(cli), "-m", str(model), "--mmproj", str(mmproj),
        "--image", str(png), "-p", prompt,
        "--temp", f"{temp:g}", "--no-warmup",
    ] if mmproj else [
        str(cli), "-m", str(model),
        "--image", str(png), "-p", prompt,
        "--temp", f"{temp:g}", "--no-warmup",
    ]
    if threads:
        cmd += ["-t", str(threads)]
    if ctx:
        cmd += ["-c", str(ctx)]

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=CREATE_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return PageResult(0, error=f"超时（>{timeout:g}s）")
    except FileNotFoundError as exc:
        return PageResult(0, error=f"无法执行 {cli}: {exc}")

    raw = proc.stdout or ""
    if proc.returncode != 0 and not raw.strip():
        tail = "\n".join((proc.stderr or "").strip().splitlines()[-6:])
        return PageResult(0, error=f"退出码 {proc.returncode}\n{tail}")
    return raw


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def parts_dir_for(out_md: Path) -> Path:
    return out_md.parent / f".{out_md.stem}.parts"


def read_part(path: Path) -> str | None:
    if path.is_file():
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return None
    return None


def write_part(path: Path, text: str) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def build_markdown(pdf: Path, pages: list[int], parts: dict[int, str], meta: dict[str, str]) -> str:
    head = ["---", f"source: {pdf.name}"]
    head += [f"{k}: {v}" for k, v in meta.items()]
    head += ["---", ""]
    body: list[str] = []
    for i, p in enumerate(pages):
        content = parts.get(p, "").strip()
        body.append(f"<!-- page {p} -->")
        body.append("")
        body.append(content if content else "> *（本页无可识别内容）*")
        body.append("")
        if i != len(pages) - 1:
            body += ["---", ""]
    return "\n".join(head + body).rstrip() + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="pdf2md",
        description="把 PDF 逐页转成 Markdown（MiniCPM-OCR + llama-mtmd-cli）。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "例子:\n"
            "  python pdf2md.py scan.pdf\n"
            "  python pdf2md.py scan.pdf -m MiniCPM-OCR-1.0-Q8_0.gguf "
            "--mmproj mmproj-MiniCPM-OCR-1.0-Q8_0.gguf -o out.md\n"
            "  python pdf2md.py book.pdf --pages 1-20 --dpi 180 --jobs 2\n"
            "  python pdf2md.py book.pdf --force            # 忽略已有分页缓存重跑\n"
        ),
    )
    ap.add_argument("pdf", help="输入 PDF 路径")
    ap.add_argument("-o", "--output", help="输出 .md 路径（默认与 PDF 同名同目录）")

    g = ap.add_argument_group("模型")
    g.add_argument("-m", "--model", help="主模型 .gguf；给目录则在其内扫描选择")
    g.add_argument("--mmproj", help="多模态投影器 .gguf（默认按文件名自动配对）")
    g.add_argument("--model-dir", action="append", default=[],
                   metavar="DIR", help="自动发现模型的目录，可重复；默认脚本目录+当前目录")
    g.add_argument("--bin", help="llama-mtmd-cli 可执行文件路径")
    g.add_argument("--non-interactive", action="store_true",
                   help="命中多个模型时不交互，直接报错")

    g = ap.add_argument_group("渲染与推理")
    g.add_argument("--dpi", type=int, default=200,
                   help="渲染 DPI 上限（默认 200，实际会被 --max-pixels 压低）")
    g.add_argument("--max-pixels", type=float, default=1.5e6,
                   help="单页像素预算，超出则等比缩小（默认 1.5MP；"
                        "模型在 ~2MP 以上会退化成空输出）")
    g.add_argument("--no-original", action="store_true",
                   help="不从 PDF 里抽扫描原图，强制重新渲染")
    g.add_argument("--pages", help="页码范围，如 1-5,8,10-（默认全部）")
    g.add_argument("--jobs", type=int, default=1, help="并行进程数（默认 1，吃显存，慎调大）")
    g.add_argument("--prompt", default=DEFAULT_PROMPT, help="识别提示词")
    g.add_argument("--temp", type=float, default=0.0, help="采样温度（默认 0）")
    g.add_argument("--threads", type=int, help="CPU 线程数（默认交给 llama.cpp）")
    g.add_argument("--ctx", type=int, help="上下文长度（默认从模型读取）")
    g.add_argument("--timeout", type=float, default=600.0, help="单页超时秒数（默认 600）")

    g = ap.add_argument_group("其他")
    g.add_argument("--keep-images", metavar="DIR", help="保留每页渲染的 PNG 到该目录")
    g.add_argument("--no-clean", action="store_true", help="不做输出清洗（调试用）")
    g.add_argument("--force", action="store_true", help="忽略分页缓存，全部重跑")
    g.add_argument("--clean", action="store_true", help="结束后删除分页缓存目录")
    g.add_argument("--keep-going", action="store_true", help="单页失败继续，不中断整份文档")
    g.add_argument("-q", "--quiet", action="store_true", help="不打印进度")

    args = ap.parse_args(argv)

    pdf = Path(args.pdf).expanduser().resolve()
    if not pdf.is_file():
        sys.exit(f"PDF 不存在: {pdf}")

    out_md = Path(args.output).expanduser().resolve() if args.output else pdf.with_suffix(".md")
    if out_md.is_dir():
        out_md = out_md / (pdf.stem + ".md")

    cli = find_cli(args.bin)
    pair = resolve_model(args)
    if not pair.mmproj:
        sys.exit(
            "找不到配套的 mmproj 投影器（MiniCPM-OCR 必需）。\n"
            "  请用 --mmproj 指定，例如 --mmproj mmproj-MiniCPM-OCR-1.0-Q8_0.gguf"
        )

    with pymupdf.open(pdf) as doc:
        total = doc.page_count
    if total == 0:
        sys.exit("PDF 没有任何页面")

    pages = parse_pages(args.pages, total)
    parts_dir = parts_dir_for(out_md)
    parts_dir.mkdir(parents=True, exist_ok=True)

    image_dir = Path(args.keep_images).expanduser().resolve() if args.keep_images else None
    if image_dir:
        image_dir.mkdir(parents=True, exist_ok=True)

    workdir = Path(tempfile.mkdtemp(prefix="pdf2md-"))

    log(f"PDF     : {pdf}  ({total} 页，处理 {len(pages)} 页)")
    log(f"模型    : {pair.model.name}")
    log(f"投影器  : {pair.mmproj.name}")
    log(f"输出    : {out_md}")
    log(f"参数    : dpi≤{args.dpi} 像素≤{args.max_pixels / 1e6:.1f}MP "
        f"jobs={args.jobs} temp={args.temp:g}"
        f"{' 强制渲染' if args.no_original else ' 优先用扫描原图'}")
    log("")

    # 缓存命中
    results: dict[int, PageResult] = {}
    todo: list[int] = []
    for p in pages:
        cached = None if args.force else read_part(parts_dir / f"page-{p:04d}.md")
        if cached is not None:
            results[p] = PageResult(page=p, text=cached)
        else:
            todo.append(p)

    if results:
        log(f"缓存命中 {len(results)} 页，待处理 {len(todo)} 页")

    t_start = time.time()
    progress = Progress(len(todo), enabled=not args.quiet)

    def work(page: int) -> PageResult:
        return ocr_page(
            cli=cli, model=pair.model, mmproj=pair.mmproj,
            doc_path=pdf, page_no=page, dpi=args.dpi,
            max_pixels=args.max_pixels, use_original=not args.no_original,
            prompt=args.prompt,
            threads=args.threads, ctx=args.ctx, temp=args.temp,
            timeout=args.timeout,
            image_out=(image_dir / f"{pdf.stem}-p{page:04d}.png") if image_dir else None,
            workdir=workdir, clean=not args.no_clean,
        )

    try:
        if todo:
            with cf.ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
                futures = {pool.submit(work, p): p for p in todo}
                for fut in cf.as_completed(futures):
                    page = futures[fut]
                    try:
                        res = fut.result()
                    except Exception as exc:  # noqa: BLE001
                        res = PageResult(page=page, error=f"内部错误: {exc}")

                    results[page] = res

                    if res.error:
                        progress.page_done(page, "✗", res.seconds)
                        progress.note(f"      └─ {res.error.splitlines()[0]}")
                        if not args.keep_going:
                            for f in futures:
                                f.cancel()
                            raise RuntimeError(f"第 {page} 页失败: {res.error}")
                    elif res.blank:
                        write_part(parts_dir / f"page-{page:04d}.md", "")
                        progress.page_done(page, "·", res.seconds)
                    else:
                        write_part(parts_dir / f"page-{page:04d}.md", res.text)
                        progress.page_done(page, "✓", res.seconds, chars=len(res.text))
    except (RuntimeError, KeyboardInterrupt) as exc:
        progress.close()
        elapsed = time.time() - t_start
        if isinstance(exc, KeyboardInterrupt):
            log(f"\n已中断（{elapsed:.1f}s）。已完成的页保存在 {parts_dir}，重跑会自动续上。")
            return 130
        log(f"\n{exc}\n已完成的页保存在 {parts_dir}，修复后重跑会自动续上。")
        return 1
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    progress.close()

    # 拼装
    body_parts: dict[int, str] = {}
    failed: list[int] = []
    for p in pages:
        r = results.get(p)
        if r is None or r.error:
            failed.append(p)
            body_parts[p] = f"> *（第 {p} 页识别失败）*"
        else:
            body_parts[p] = r.text

    meta = {
        "pages": f"{pages[0]}-{pages[-1]}" if len(pages) == total else f"{len(pages)}/{total}",
        "model": pair.model.name,
        "dpi": str(args.dpi),
        "max_pixels": f"{args.max_pixels / 1e6:.1f}MP",
        "created": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_md.write_text(build_markdown(pdf, pages, body_parts, meta), encoding="utf-8")

    if args.clean:
        shutil.rmtree(parts_dir, ignore_errors=True)

    elapsed = time.time() - t_start
    total_chars = sum(len(v) for v in body_parts.values())
    log("")
    log(f"完成: {out_md}")
    done_pages = len(todo)
    if done_pages:
        log(f"      共 {len(pages)} 页 / {total_chars} 字符 / 耗时 {_fmt_dur(elapsed)}"
            f"（{done_pages} 页新处理，平均 {elapsed / done_pages:.1f}s/页）")
    else:
        log(f"      共 {len(pages)} 页 / {total_chars} 字符 / 全部命中缓存，耗时 {_fmt_dur(elapsed)}")
    if failed:
        log(f"      ⚠ 失败页: {failed}（重跑会重试这些页）")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())