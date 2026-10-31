# pdf2md

扫描版 PDF 转 Markdown，公式输出 LaTeX。跑的是 MiniCPM-OCR 加 llama.cpp。

适合教材、习题集、讲义这类公式密集的扫描件。文字版 PDF 别绕这一圈，PyMuPDF 直接抽文本又快又准。

## 特性

- 逐页识别，每页结果立刻落盘。中途断了重跑，已完成的自动跳过
- 单页尺寸超限时自动缩小重试，不会卡在空输出上
- 清洗输出：去掉寒暄、代码围栏、错包成公式的正文、模型自带的表格标记
- 扫描件直接取原图，少一道重采样
- 终端有进度条、实时速度和预计剩余时间
- 支持页面范围、并发、断点续跑

## 环境要求

Python 3.9 以上，另外三样东西得自己准备。

### Python 包

```bash
pip install pymupdf
pip install numpy pillow
```

`pymupdf` 是必须的。`numpy` 和 `Pillow` 只在抽取扫描原图时用到，没装也能跑，会退回渲染路径，画质差一点。

### llama.cpp

<https://github.com/ggml-org/llama.cpp/releases>

下载对应后端的压缩包解压。NVIDIA 选 CUDA，Intel 和 AMD 选 Vulkan，什么显卡都没有就用 CPU 版。

里面要有 **`llama-mtmd-cli.exe`**。

旧版本的包里只有 `llama-cli.exe`，那个不接受图片输入，得换个新一点的版本（b5xxx 之后基本都有）。

想自己编译：

```bash
cmake -B build && cmake --build build --config Release
```

产物在 `build/bin/`。

### 模型

<https://hf-mirror.com/AtomicChat/MiniCPM-OCR-1.0-GGUF>

两个文件都要，缺一个跑不起来。

| 文件 | 大小 | 作用 |
|---|---|---|
| `MiniCPM-OCR-1.0-Q8_0.gguf` | 775 MB | 主模型 |
| `mmproj-MiniCPM-OCR-1.0-Q8_0.gguf` | 589 MB | 视觉投影器 |

国内直连 HuggingFace 很慢，走镜像：

```bash
pip install -U "huggingface_hub[cli]"
export HF_ENDPOINT=https://hf-mirror.com
hf download AtomicChat/MiniCPM-OCR-1.0-GGUF --local-dir models
```

PowerShell 里第一行改成 `$env:HF_ENDPOINT = "https://hf-mirror.com"`。

不想装命令行工具就直接开上面那个网页，手动点两个 `.gguf` 下载。

## 安装

两种方式任选。

### 直接下 exe（Windows，不用装 Python）

release 页面里有打包好的 `pdf2md.exe`，45 MB，双击没用，得在命令行跑。跟模型、llama.cpp 摆在一起就行：

```
pdf2md/
├── pdf2md.exe
├── models/
│   ├── MiniCPM-OCR-1.0-Q8_0.gguf
│   └── mmproj-MiniCPM-OCR-1.0-Q8_0.gguf
└── llama-b12020-bin-win-vulkan-x64/
    └── llama-mtmd-cli.exe
```

然后在那个目录下开终端：

```bash
pdf2md.exe 高等数学.pdf
```

exe 每次启动要自解压，比跑脚本慢两三秒，识别速度一样。杀毒软件可能会拦一下，加信任即可。

### 用 Python 跑

下载 `pdf2md.py`。文件放哪都行，但按同样的布局摆可以完全不传参数。

脚本查找顺序：自己的目录 → 当前目录 → `PATH`。模型和 llama.cpp 放在别处的话，用 `-m` 和 `--bin` 指过去。

模型和投影器按文件名配对，`mmproj-` 前缀去掉后同名就算一对。所以 Q8_0 的主模型不会错配到 Q4 的投影器。目录里有多个模型时会列出来让你选，加 `--non-interactive` 就直接报错。

## 快速开始

```bash
python pdf2md.py 高等数学.pdf     # 用 Python 跑
pdf2md.exe 高等数学.pdf           # 用打包好的 exe
```

输出到同目录同名 `.md`。

先拿几页试水，看看效果再跑全书：

```bash
python pdf2md.py 高等数学.pdf --pages 100,150,200 -o 试水.md
python pdf2md.py 高等数学.pdf -o 全书.md
```

## 用法

### 指定模型位置

```bash
python pdf2md.py book.pdf \
    -m D:/models/MiniCPM-OCR-1.0-Q8_0.gguf \
    --mmproj D:/models/mmproj-MiniCPM-OCR-1.0-Q8_0.gguf \
    --bin D:/llama/llama-mtmd-cli.exe
```

`-m` 给目录也行，会在里面扫 `.gguf`。

### 只转一部分

```bash
# 第 1 到 20 页，加第 35 页，加第 50 页到末尾
python pdf2md.py book.pdf --pages 1-20,35,50-
```

### 其它常用组合

```bash
python pdf2md.py book.pdf --jobs 2              # 并行，吃显存，先试 2
python pdf2md.py book.pdf --keep-images ./imgs  # 保留每页 PNG，方便查错
python pdf2md.py book.pdf --clean               # 跑完删分页缓存
python pdf2md.py book.pdf --keep-going          # 单页失败不中断
python pdf2md.py book.pdf --pages 10 --no-clean # 看模型原始输出
```

## 参数

### 模型

| 参数 | 说明 | 默认 |
|---|---|---|
| `-m, --model` | 主模型 `.gguf` 或所在目录 | 自动发现 |
| `--mmproj` | 视觉投影器 `.gguf` | 按文件名配对 |
| `--model-dir DIR` | 追加搜索目录，可重复 | 脚本目录 + 当前目录 |
| `--bin` | `llama-mtmd-cli` 路径 | 自动发现 |
| `--non-interactive` | 多个模型时不询问，直接报错 | 关 |

### 渲染与推理

| 参数 | 说明 | 默认 |
|---|---|---|
| `-o, --output` | 输出路径 | 与 PDF 同名 |
| `--dpi` | 渲染 DPI 上限 | `200` |
| `--max-pixels` | 单页像素上限 | `1.5e6` |
| `--no-original` | 不抽扫描原图，强制重渲染 | 关 |
| `--pages` | 页码范围 | 全部 |
| `--jobs` | 并行进程数 | `1` |
| `--prompt` | 识别提示词 | 见下 |
| `--temp` | 采样温度 | `0` |
| `--threads` | CPU 线程数 | 交给 llama.cpp |
| `--ctx` | 上下文长度 | 从模型读 |
| `--timeout` | 单页超时（秒） | `600` |

### 其它

| 参数 | 说明 |
|---|---|
| `--keep-images DIR` | 保留每页 PNG |
| `--no-clean` | 不清洗输出，调试用 |
| `--force` | 忽略缓存全部重跑 |
| `--clean` | 结束后删缓存目录 |
| `--keep-going` | 单页失败继续 |
| `-q, --quiet` | 不打印进度 |

### 默认提示词

```text
OCR this page. Transcribe every character you see verbatim, preserving the
original layout and reading order. Never answer, solve, or explain any
problem. Output all mathematical formulas in LaTeX format.
```

`Never answer, solve, or explain any problem` 这句别删。删了模型就开始给你解数学题，输出一大段推导过程，正文字一个没有。

## 输出

带 YAML 头，页与页之间用注释标记：

```markdown
---
source: 高等数学 上册 第八版.pdf
pages: 121-122
model: MiniCPM-OCR-1.0-Q8_0.gguf
dpi: 200
max_pixels: 1.5MP
created: 2026-10-31 20:40:02
---

<!-- page 121 -->

第三章 微分中值定理与导数的应用

例 1 设 $f(x) = x^2$，求 $f'(x)$。

$$\lim_{\Delta x \to 0} \frac{f(x_0 + \Delta x) - f(x_0)}{\Delta x}$$

---

<!-- page 122 -->
```

识别失败的页留一行 `> （第 N 页识别失败）`，空白页留 `> （本页无可识别内容）`。都不影响其它页。

### 分页缓存

每页结果写在 `.<输出名>.parts/page-NNNN.md`，跟输出的 md 放一起。这是隐藏目录，跑完用 `--clean` 清掉，或者自己删。

中断之后重跑同一条命令，已完成的页直接命中，零耗时。想全部重来加 `--force`。

## 几个坑

**模型会把教材当习题做。** 症状是某页跑了一分钟还没完，输出几千字英文推导。默认提示词已经加了禁止语，别改掉。

**页面太大模型就罢工。** 像素超过 2MP 左右，它会稳定输出一个空的 think 标签就退出。不随机，`--temp 0` 下重复四次结果一样。脚本先按 1.5MP 缩放，还空就再打八折，最多两次。

**扫描件优先取原图。** 一整页就是一张 JPEG 的情况很常见，直接拿出来转正、缩放一次，比重新渲染干净。加 `--no-original` 可以关掉。

**进度走 stderr。** 别用 `python pdf2md.py x.pdf | tail`，管道缓冲会把进度全吞掉，看起来像卡死。要看实时进度就重定向到文件再 `tail -f`。

## 性能

Core Ultra 5 338H 自带的 Arc B370 核显（Vulkan 后端）+ Q8_0，扫描版高等数学教材：

| 场景 | 耗时 |
|---|---|
| 公式密集页 | 13 到 15 秒 |
| 纯文字页 | 6 到 10 秒 |
| 空白页（直接跳过） | 0.1 秒 |
| 442 页全书 | 约 1.6 小时 |

核显跑出来的，不用独显。只有这一台机器的数据，显卡不同会有出入，纯 CPU 慢一个数量级。

## 常见问题

**提示找不到 llama-mtmd-cli**
解压出来的包里没有这个文件说明版本太旧，只有 `llama-cli.exe`。换个新 tag，或者用 `--bin` 直接指。

**提示找不到模型**
模型得和投影器一起放。只放主模型跑不了视觉，会报错。

**某页输出是空的或乱码**
先用 `--keep-images` 导出 PNG 看看图本身是不是空白或者歪的。图正常但没输出，降 `--max-pixels 1.2e6` 再试。

**显存不够**
`--jobs 1`，降 `--max-pixels`。核显是跟系统内存共享的，内存紧张时一样会失败，先把后台吃内存的程序关掉。NVIDIA 用户也可以换 CUDA 后端，一般比 Vulkan 快一些。

**中文乱码**
程序内部统一 UTF-8。终端不是的话执行 `chcp 65001`。

## 致谢

- [llama.cpp](https://github.com/ggml-org/llama.cpp)
- [MiniCPM-OCR](https://hf-mirror.com/AtomicChat/MiniCPM-OCR-1.0-GGUF)
- [PyMuPDF](https://pymupdf.readthedocs.io/)

## License

[MIT](LICENSE)。

仓库里只有 `pdf2md.py` 一个脚本，不含 llama.cpp 的二进制，也不含模型权重。那些都要自己下载，遵守各自的许可：llama.cpp 本身是 MIT；MiniCPM 系列有独立的使用协议，商用前扫一眼就行。

