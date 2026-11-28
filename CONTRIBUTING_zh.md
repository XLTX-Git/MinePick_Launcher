# 参与 MinePick Launcher 的开发

MinePick Launcher 是一个用 Python + PySide6 写的便携版 Minecraft 启动器。代码、测试、文档、翻译、缺陷报告
都欢迎。这份文件写的是这个项目**实际怎么运转**，这样一份改动可以被评审、测试并发布，而不用再来回问一轮。

English version: [CONTRIBUTING.md](CONTRIBUTING.md)

## 动手之前

- 先读 [README_zh.md](README_zh.md) 和 [`docs/`](docs/) 下的文档。
- 项目使用 **GPL-3.0-only**。提交改动即表示你同意按同一许可证分发。
- 讨论留在 issue 与 pull request 里。骚扰、人身攻击、公开他人数据都不被接受。
- **安全漏洞不要开公开 issue** —— 见[安全](#安全)。

## 准备环境

需要 Windows 10 / 11（64 位）和 Python 3.11 或更高版本。

```powershell
git clone https://github.com/XLTX-Git/MinePick_Launcher.git
cd MinePick_Launcher
pip install -r requirements-dev.txt
python -m gui
```

测试是**无界面**运行的。跑测试前先设置平台插件，否则 GUI 测试会往你的桌面上弹窗口：

```powershell
$env:QT_QPA_PLATFORM = "offscreen"
```

## 三条必须通过的命令

跑多少取决于你改了什么：

```powershell
ruff check launcher gui tests            # 静态检查，每次都要
python tools/ui_regression.py --quick    # 改了界面就要跑；共七项检查
pytest -q                                # 全量测试，约 20 分钟
```

- **只改文档**（README、docs、CONTRIBUTING）：**不用跑 pytest**。跑
  `python tools/check_readme_sync.py` 与 README 相关测试（`pytest tests/test_readme_page_header_claim.py`）
  ——那才是这些文件能弄坏的东西。
- **开发过程中**：只跑你动过的那几个测试文件。单个文件是秒级，这才是你需要的循环。
- **标记 pull request 为「待合并」之前**：跑一次全量 `pytest -q`（约 20 分钟）。上面的 lint 与界面检查
  任何改动都要跑。

`--quick` 会跳过截图。发版门槛跑的是完整脚本，所以只过 `--quick` 的界面改动还不算完成——但足以开一个
pull request。

## 测试

- **测试默认不联网。** `tests/conftest.py` 里的自动夹具会切断 HTTP 缓存、Modrinth 资源识别、更新检查
  以及所有模态对话框。需要网络答案的测试要自己 mock（开发依赖里有 `respx`）。
- 这个环境里 `tmp_path` 不可写；需要文件时用 `ws_tmp` 夹具。
- GUI 测试用 `app` 夹具。**绝不要显示真实窗口**：自动夹具一旦发现有窗口落到桌面上就会让整轮测试失败。
- 断言要钉住用户能观察到的行为，而不是实现细节。刻意的行为变更把测试弄红时，要在**同一个提交**里改测试，
  并在 docstring 里写清楚新的行为。
- 已知 flake：GUI 工作线程偶尔原生崩溃，干净的提交也能复现。整轮重跑一次；仍红就分半跑
  （`-k "not java and not locate and not detected"` 与其补集）。两侧都绿 → 视为 flake，并在提交信息末尾加
  `(flake rerun passed)`；任一侧单独重跑仍崩 → 按真失败处理，不得提交。

## 代码

- 检查与格式配置在 `pyproject.toml` 里，跑 `ruff check` 并按它的判断来。
- **每个新增源码文件都要带 GPL 头**——`SPDX-FileCopyrightText`、`SPDX-License-Identifier: GPL-3.0-only`、
  "This file is part of MinePick Launcher." 一句以及完整授权段落。从已有模块复制即可；缺了它过不了回归检查。
- 公开函数要写类型标注。注释解释**为什么**这样写，而不是下一行在做什么。
- 新增依赖要先核对许可证（必须与 GPL 兼容），并跑一次 `python tools/build_third_party_notices.py`，
  让 `THIRD_PARTY_NOTICES.md` 跟上。

## 文案与翻译

- 不允许硬编码用户可见的文字。界面文字走 `gui/i18n.py` + `gui/i18n_langs.py`；核心（非界面）文字走
  `launcher/i18n.py` + `launcher/i18n_langs.py`。
- 界面共九种语言：`zh_cn`、`zh_tw`、`en_us`、`ja_jp`、`ko_kr`、`ru_ru`、`fr_fr`、`es_es`、`de_de`。
  新增一个键就要在九张表里都补上。缺翻译会回退到英文，那是缺陷，不是省事的办法。
- README 有九种语言。`python tools/check_readme_sync.py` 必须报 `0 drift`；
  `pytest tests/test_readme_page_header_claim.py` 会钉住「README 不得再承诺已经删掉的界面」。
- 技术文档成对出现，命名与 README 同向：`docs/x.md` 是**英文**，`docs/x_zh.md` 是**中文**，两边都要改。

## 界面改动

- 可点目标至少 24×24 像素；每个可交互控件都有可读名称；键盘焦点始终可见。颜色取自主题令牌，
  两套主题 × 八个强调色的对比度都在审计范围内。
- 窗口下限是 1000×610。页面不得新增本来不需要的滚动条，标签页也不能滚过自己的内容。
- 页面有变化时重新生成截图：`python tools/shot_pages.py --root . --preview . --out docs/screenshots
  --language zh_cn`，再用 `--language en_us` 跑一遍（脚本必须带 `--root` 和 `--out`，只写
  `python tools/shot_pages.py` 会直接报参数错误）。目前只有 `zh` 和 `en` 两套截图，
  所以翻译版 README 会注明截图显示的是哪个界面。

## 已经踩过的 Qt 坑

下面每一条都花过调试时间；先知道，能省下一次。

- **样式表作用于窗口，不作用于整个应用。** `QComboBox` 的下拉列表自己就是一个顶层窗口，所以
  `gui/popup_shape.py` 单独给它上样式，并用委托绘制列表项：平台自带委托会忽略 `::item` 的 padding，
  样式表里写 `padding` 和 `height` 在那里不起作用。
- **`QStackedWidget.sizeHint()` 取的是所有页面里的最大值。** 实例详情页因此自己算滚动几何
  （`_FitStack` 加 `_sync_content_geometry`）；有布局的页面用 `layout.heightForWidth(width)` 取高度，
  而 `sizeHint()` 按理想宽度回答，会把自动换行的卡片算高。
- **不要把 `QPushButton` 塞进另一个按钮的布局里。** 内层按钮会继承外层高度，文字被挤进很小的内容区——
  测速按钮的标签曾经被裁得只剩几个像素。
- **`clicked` 会带一个 bool。** 写 `lambda group=key: ...` 时第一个参数会静默收到 `False`，要写成
  `lambda *_, group=key: ...`。
- **控件还没显示时读 `sizeHint()` 会得到 0 或过期值。** `LoaderVersionItem` 因此直接返回固定高度。
- **另一个线程正在读目标文件时，`Path.replace` 会抛 `PermissionError`。** 镜像测速那条路径带重试，
  别把重试删掉。
- **一律按 UTF-8 写，且写文件带 `newline=""`。** 少了它，样式表或 JSON 会被写成 CRLF，钉住摘要的
  测试就会在一个没人改过的差异上变红。

## 产品红线

这些是行为约定，不是偏好；破坏了就是回归，哪怕测试全绿。

- 模组与资源包的图标**只能**来自哈希或指纹匹配（Modrinth sha1、CurseForge MurmurHash2），绝不按名字猜。
- Terracotta（AGPL-3.0 带例外）与 EasyTier（LGPL-3.0）以未修改的二进制形式放在 `components/`。
  页脚的 Terracotta 署名是必须的（原文在 `launcher/notices.py`）；`components/easytier` 绝不能被
  「关闭联机」的代码删掉——网络自检依赖它。
- 关闭联机大厅意味着隧道停止、客人断开；本启动器启动的核心一并结束，借用的核心不动。
- 长耗时下载要做成任务（`gui/tasks.py`）：`TaskCenter.add_task` 带取消路径，而不是只写一行状态。
- 已安装的 Java 运行时必须真的能启动（`java -version`），否则移除并报告；绝不要返回没探活过的运行时。

## 提交与 pull request

- **提交信息只用英文**——中英混写会在评审里被退回。用翻译工具翻一下就可以：
  `fix: resolve the launcher crash on startup` 是对的，`fix: 修复启动崩溃` 不对。
- 用约定前缀：`feat`、`fix`、`docs`、`test`、`chore`、`refactor`、`perf`、`style`。
- 标题用祈使句，不超过 72 个字符。正文说明改了什么、**为什么**改，并写出你跑过的命令
  （例如「pytest tests/test_tools_page.py: 63 passed」）。
- 一个提交只做一件事。一次改了两个不相干的地方，就该拆成两个提交。

## 发版

- 版本号只在**真正发版时**才升，不要让未发布的版本号躺在仓库里。规则与升位表见
  [`docs/github_release.md`](docs/github_release.md)。
- 发版门槛是 `python tools/ui_regression.py --quick` 全部 OK，再加上对打包产物 EXE 与 `Source_code.zip`
  的开箱核验（包内版本号、九份 README、无 `tests/.work` 残留、逐文件 SHA-256）。

## 报告缺陷

请附上：启动器版本（「关于」页）、Windows 版本、你做了什么、发生了什么，以及「诊断」页导出的报告
（它会脱敏访问令牌与用户路径）。启动器一侧的故障带 `MPL-Launch-####` 错误码，游戏崩溃带
`MPL-Crash-####`；光是这个码往往就能定位原因。

## 安全

发现漏洞**不要**开公开 issue。用 GitHub 的私有 *Report a vulnerability*（Security → Advisories），
或直接联系维护者。除非你要求匿名，我们会在修复说明里署名致谢。

## 第三方组件

Terracotta（AGPL-3.0，带例外条款）与 EasyTier（LGPL-3.0）以**未修改的二进制**形式随包放在
`components/` 下。它们的许可证文本、源码获取方式以及界面上必须保留的署名
（「联机由 Terracotta | 陶瓦联机 提供」）都不是可选项；「停用联机功能」的代码路径也**不得删除**
`components/easytier`——网络自检依赖它。
