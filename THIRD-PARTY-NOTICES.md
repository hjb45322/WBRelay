# 第三方组件声明

本文件列出**打包后的 Windows 可执行文件**（`WBRelay.exe`）内含的第三方组件及其许可。
WBRelay 自身以 MIT 发布（见 `LICENSE`）；下列组件的版权归各自作者所有。

> 为什么需要这份文件：`WBRelay.exe` 是 PyInstaller 单文件包、**运行时不需要安装 Python** ——
> 代价是它把 CPython、窗口/托盘依赖与图像库一并冻结进了这个 EXE。分发该二进制时应随附本文件。

## 从源码使用不涉及本文件

`pip install .`（网关核心，**只用标准库**）或 `pip install ".[gui]"`（额外要独立窗口 / 系统托盘）时，
下列组件由使用者自行从 PyPI 安装，适用于它们各自的许可 —— 与对本仓库源码的使用无关。

## EXE 内含组件

| 组件 | 许可 | 版权与全文 |
|---|---|---|
| CPython | PSF License | © Python Software Foundation — https://docs.python.org/3/license.html |
| PyInstaller | GPLv2-or-later，**附例外条款**：允许用它打包并分发非自由程序 | © PyInstaller Development Team — https://github.com/pyinstaller/pyinstaller/blob/develop/COPYING.txt |
| Pillow | MIT-CMU (HPND) | © 2010-2025 Jeffrey A. Clark 与贡献者 — https://github.com/python-pillow/Pillow/blob/main/LICENSE |
| pywebview | BSD 3-Clause | © Roman Sirokov 与贡献者 — https://github.com/r0x0r/pywebview/blob/master/LICENSE |
| pystray | **LGPLv3** | © Moses Palmér — https://github.com/moses-palmer/pystray/blob/master/COPYING |

2026-10-14 打包时实测版本：CPython 3.9.13 / PyInstaller 6.22.3 / Pillow 11.3.0 /
pywebview 6.2.1 / pystray 0.19.5（重新打包时以实际为准）。

## 关于 pystray（LGPLv3）—— 如实说明

LGPLv3 要求分发方让接收方**能够替换**该库；而被冻结进单文件 EXE 时无法替换 —— 这是所有
Python 冻结应用共有的灰区。本项目的处理方式：

- 提供**源码运行方式**：任何人都可以 `pip install ".[gui]"` 后安装、替换、升级任意版本的
  pystray / pywebview / Pillow，从而完全绕开冻结问题；
- 需要更严格合规的分发者，可在打包时排除 GUI 依赖，得到一个无托盘版本
  （打包时设 `excludes=['pystray', 'webview', 'PIL']`）；此时 EXE 只剩 CPython 与
  PyInstaller 引导器，几乎没有额外义务。缺少 GUI 依赖时程序会自动降级为浏览器模式。

## 报告问题

若你认为本文件遗漏了某个组件、或声明不准确，请开 issue 说明。
