# Third-party notices / 第三方许可声明

MinePick Launcher 0.5.0 — <https://github.com/XLTX-Git/MinePick_Launcher>

MinePick Launcher itself is released under **GPL-3.0-only** (see [LICENSE](LICENSE)). It uses the third-party components and services listed below; their own licences and terms apply to those parts. Versions are read from the installed distribution metadata.

Qt for Python (PySide6) is licensed under **LGPL-3.0** and is shipped as a dynamically loaded library that the launcher does not modify. The single-file build unpacks those Qt libraries from the archive into a temporary directory when it starts; Qt's source code is available from <https://code.qt.io/> and from the Qt release archives at <https://download.qt.io/official_releases/qt/>. The About page inside the application shows the same list, translated into the interface language.

MinePick Launcher 本体以 **GPL-3.0-only** 发布（见 [LICENSE](LICENSE)）。下列第三方组件与服务各自适用其自身许可与条款；版本号取自实际安装的发行版元数据。Qt for Python（PySide6）以 **LGPL-3.0** 授权，以动态库形式提供且未作修改；单文件构建在启动时会把这些 Qt 库从压缩包解压到临时目录。Qt 源码可从 <https://code.qt.io/> 与 <https://download.qt.io/official_releases/qt/> 获取。应用内的「关于」页展示同一份清单（按界面语言翻译）。

EasyTier is **not bundled** in this repository or in the installer. The network check downloads the official v2.6.4 Windows archive at runtime, verifies its SHA-256, installs only the unmodified `easytier-cli.exe`, and can replace it at any time. EasyTier is licensed under **LGPL-3.0**; the complete licence text is at <https://www.gnu.org/licenses/lgpl-3.0.txt> and the corresponding source for the installed version is at <https://github.com/EasyTier/EasyTier/tree/v2.6.4>. Anyone who redistributes the downloaded CLI must include this notice, the LGPL-3.0 text and a way to obtain that source.

EasyTier **不随**本仓库或安装包分发。网络自检在运行时下载官方 v2.6.4 Windows 压缩包，校验 SHA-256 后只安装未修改的 `easytier-cli.exe`，并可随时替换该文件。EasyTier 以 **LGPL-3.0** 授权；完整许可文本见 <https://www.gnu.org/licenses/lgpl-3.0.txt>，所安装版本对应的源码见 <https://github.com/EasyTier/EasyTier/tree/v2.6.4>。再分发该 CLI 时，必须保留本声明、LGPL-3.0 许可文本以及获取对应源码的途径。

Minecraft block textures - used only as version icons - belong to Mojang. MinePick Launcher does **not distribute** these images. The icon source order is: the user's own ``assets`` / ``client.jar`` → the user's local cache → fetched at runtime from mcmod.cn's image host and cached in the user's configuration directory. Neither the source tree nor the installer contains a Minecraft image.

原版方块材质（仅用作版本图标）归 Mojang 所有；本项目不分发这些图片。图标的来源顺序为：用户本机的 ``assets`` / ``client.jar`` → 用户本机缓存 → 运行时从 mcmod.cn 的图床获取并缓存到用户配置目录。源码与安装包都不包含任何 Minecraft 图片。

The names and logos of the mod loaders (Fabric, Forge, NeoForge, Quilt) belong to their respective projects. MinePick Launcher does **not distribute** these logos: neither the source tree nor the installer contains one. They are fetched at runtime from their official websites and cached in the user's local configuration directory (NeoForge's mark comes from the official NeoForged art repository on GitHub); when a logo cannot be fetched, the interface shows its own drawn substitute icon.

各模组加载器的名称与标志归其各自项目所有；本项目不在分发包内包含这些标志，仅在运行时从其官方网站获取并缓存到用户本地配置目录，取不到时显示自绘的替代图标。NeoForge 的标志取自 NeoForged 官方美术仓库。

## Dependencies / 依赖项

### Runtime dependencies / 运行时随程序分发

- **Qt for Python (PySide6)** — `LGPL-3.0-only` — installed: PySide6 6.11.1, shiboken6 6.11.1
  - the interface framework
  - 界面框架
  - <https://doc.qt.io/qtforpython-6/>
- **httpx** — `BSD-3-Clause` — installed: httpx 0.28.1
  - the HTTP client
  - HTTP 客户端
  - <https://www.python-httpx.org/>
- **msal** — `MIT` — installed: msal 1.37.0
  - Microsoft account sign-in
  - 微软账号登录
  - <https://github.com/AzureAD/microsoft-authentication-library-for-python>
- **pydantic** — `MIT` — installed: pydantic 2.13.4
  - configuration and data models
  - 配置与数据模型
  - <https://docs.pydantic.dev/>
- **platformdirs** — `MIT` — installed: platformdirs 4.11.2
  - cross-platform directories
  - 跨平台目录
  - <https://github.com/platformdirs/platformdirs>
- **tenacity** — `Apache-2.0` — installed: tenacity 9.1.4
  - retrying failed calls
  - 失败重试
  - <https://github.com/jd/tenacity>
- **rich** — `MIT` — installed: rich 15.0.0
  - coloured command-line output
  - 命令行彩色输出
  - <https://github.com/Textualize/rich>
- **packaging** — `Apache-2.0 or BSD-2-Clause` — installed: packaging 26.3
  - version parsing and comparison
  - 版本号解析与比较
  - <https://github.com/pypa/packaging>
- **cryptography (with OpenSSL)** — `Apache-2.0 or BSD-3-Clause` — installed: cryptography 50.0.0
  - token encryption and certificate handling (including OpenSSL)
  - 令牌加密与证书校验（含 OpenSSL）
  - <https://cryptography.io/>
- **truststore** — `MIT` — installed: truststore 0.10.4
  - using the operating system's certificate store
  - 使用系统证书存储
  - <https://github.com/sethmlarson/truststore>
- **Lucide** — `ISC`
  - sidebar navigation icons
  - 侧栏导航图标
  - <https://lucide.dev/>
- **anyio 4.14.2, certifi 2026.7.22, httpcore 1.0.9, h11 0.16.0, idna 3.18**
  - pulled in by httpx; no separate notice applies, their licences are listed per package in the distribution metadata
  - 由 httpx 引入；各自的许可条款见对应发行版的元数据

### Build & test tools / 仅构建与测试时使用

- **pytest, respx, ruff, PyInstaller** — `MIT / BSD-3-Clause / GPL-2.0 with exception` — installed: pytest 9.1.1, respx 0.23.1, ruff 0.16.3, pyinstaller 6.22.0
  - build-time tools, not shipped with the launcher
  - 开发期工具，不随程序分发

## Services and data / 服务与数据来源

- **Mojang & Microsoft**
  - the official version manifest, asset library and sign-in services
  - 官方版本清单、资源库与登录服务
  - <https://www.minecraft.net/>
- **Modrinth**
  - mod, resource-pack, shader and modpack search
  - 模组、资源包、光影与整合包检索
  - <https://modrinth.com/>
- **CurseForge**
  - mod search
  - 模组检索
  - <https://www.curseforge.com/minecraft>
- **BMCLAPI**
  - Minecraft resource mirror service (mainland-China acceleration)
  - Minecraft 资源镜像服务（国内加速）
  - <https://github.com/bangbang93/BMCLAPI>
- **Fabric**
  - the Fabric loader and its metadata
  - Fabric 加载器与其元数据
  - <https://fabricmc.net/>
- **Forge**
  - the Forge loader and its Maven repository
  - Forge 加载器与其 Maven 仓库
  - <https://files.minecraftforge.net/>
- **NeoForge**
  - the NeoForge loader and its Maven repository
  - NeoForge 加载器与其 Maven 仓库
  - <https://neoforged.net/>
- **Adoptium (Eclipse Temurin)**
  - Java runtime downloads
  - Java 运行时下载
  - <https://adoptium.net/>
- **GitHub**
  - code hosting, releases and the update check
  - 代码托管、发布与更新检查
  - <https://github.com/>
- **Terracotta | 陶瓦联机** — `AGPL-3.0-or-later with exception`
  - Multiplayer; the unmodified binary is used through its local HTTP API, and its copyright is credited in the interface.
  - 联机功能。以未修改的二进制形式通过本地 HTTP API 交互，界面已标注其版权。
  - <https://github.com/burningtnt/Terracotta>
- **EasyTier** — `LGPL-3.0`
  - UDP/TCP NAT-type probe for the network check. The unmodified official CLI is downloaded on first use; LGPL-3.0, with the licence text and source link below.
  - 网络自检的 UDP/TCP NAT 类型检测。首次使用时下载未修改的官方 CLI；LGPL-3.0，许可文本与源码地址见下。
  - <https://github.com/EasyTier/EasyTier>

## Inspiration and community / 灵感与社区

- **Plain Craft Launcher 2 (PCL2)**
  - the inspiration for the information architecture of this launcher's about/update pages. The technology stacks differ (C#/WPF versus Python/PySide6); this project is independently implemented and contains none of the upstream source code.
  - 本项目的「关于 / 更新」页面信息架构参考了它。技术栈不同（C#/WPF 与 Python/PySide6），本项目完全独立实现，未包含上游源代码。
  - <https://github.com/Meloong-Git/PCL>
- **PCL Community Edition (PCL CE)**
  - the community edition; this project's interface took its cues from it. The technology stacks differ (C#/WPF versus Python/PySide6); this project is independently implemented and contains none of the upstream source code.
  - 社区版；本项目的界面参考了它。技术栈不同（C#/WPF 与 Python/PySide6），本项目完全独立实现，未包含上游源代码。
  - <https://github.com/PCL-Community/PCL-CE>
- **MinePick Launcher Classic**
  - the predecessor of this launcher, archived.
  - 本启动器的前身，已归档。
  - <https://github.com/XLTX-Git/MinePick_Launcher_Classic>
- **TheDarkLord234**
  - community member and author of MinePick Launcher Revision — thanks for exploring this space together.
  - 社区伙伴，《MinePick Launcher Revision》的作者，感谢在生态探索上的并肩前行。
  - <https://github.com/TheDarkLord234>
- **Prism Launcher**
  - formerly provided the default Microsoft sign-in client id; the project now uses its own app registration.
  - 曾经提供了默认的微软登录 Client ID（现已换成本项目自己的应用注册）。
  - <https://prismlauncher.org/>
