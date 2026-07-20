# 万国觉醒 · 国士无双题库

本地题库 + 截图识题工具。上传/粘贴答题截图，自动 OCR 并匹配正确答案。

## 给没有 Python 的用户

安装包是**自包含**的：不需要安装 Python、不需要 pip 依赖。

对方电脑只需：
1. Windows 10/11
2. **Microsoft Edge WebView2**（Win10/11 通常已自带；没有时软件会弹窗提示安装）

验证命令（开发者本机）：

```bash
python scripts/smoke_packaged.py
```

## 安装包 / EXE（桌面软件窗口）

运行后是**独立软件窗口**（类似币安广场），不会打开系统浏览器。

产物在 `dist/installer/`：

- `万国觉醒题库_安装包_v1.0.0.exe`：双击安装
- `万国觉醒题库_便携版_v1.0.0.zip`：解压即用

开发模式启动桌面窗口：

```bash
pip install -r requirements.txt
python desktop.py
```

仅调试后端（可选开浏览器）：

```bash
python app.py --browser
```

重新打包：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\build_installer.ps1
```

## 快速开始（开发模式）

```bash
pip install -r requirements.txt
python app.py
```

浏览器打开：http://127.0.0.1:8765

## 用法

1. 游戏里截图（题目+选项界面）
2. 拖到页面，或 `Ctrl+V` 粘贴
3. 点「识别并答题」

也可在搜索框输入题目关键词直接查答案。

## 题库

- `data/questions.json`：完整题库（中英合并，约 2600+）
- `data/questions.zh.json`：中文题为主
- 数据来源：公开整理的知识广场 / Lyceum of Wisdom 问答（rbtips、GitHub 等）

重新构建题库：

```bash
python scripts/build_bank.py
```

补充新题：编辑 `data/questions.json` 的 `questions` 数组，或改 `scripts/build_bank.py` 里的 `extras` 后重建。

## 说明

游戏会持续加新题，公开题库无法保证 100% 覆盖。识别失败时用关键词搜索；确认答案后可手动写入题库，方便下次匹配。
