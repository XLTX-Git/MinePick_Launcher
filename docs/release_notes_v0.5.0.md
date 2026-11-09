## ✨ New

- **Multiplayer, built in.** A new 工具 page carries a 联机 tab that wraps Terracotta: host a lobby from a world you have opened to LAN, or join one with a lobby code. The card shows the code, the member list and the hole-punching difficulty while connecting. The room survives closing the launcher — a capsule in the title bar brings you back to it, including after a restart.
- **Network self-check** in the same tab reports the UDP and TCP NAT types, so "should I host?" has an answer before you try.
- **Modpack import** for Modrinth `.mrpack`, CurseForge zips and MultiMC/Prism instances: pick the archive, and the launcher installs the version, the loader and the files, then creates an instance pointing at them.
- **Modpack export**: an instance can be written out as a standard `.mrpack` or CurseForge pack that other launchers can import.
- **Crash analysis, rebuilt.** Thirty causes, mod attribution read from the stack trace, a confidence level, evidence lines with file and line number, and actions (disable this mod, open the mods folder, change the heap). It appears as an in-window card when the game crashes.
- **Launcher self-update**: checks in the background, downloads, verifies the release digest, replaces the executable in place and restarts; a failed replace rolls back.
- **Schematic browser**: an instance's `.litematic` files get their own tab, with dimensions, block kinds and the file's metadata.
- **Screenshots and server list** are tabs of an instance now: the gallery reads both the instance folder and the shared one, and the server list reads and writes `servers.dat` atomically.

## 🔧 Improvements

- The instance list uses 88px rows instead of large cards: six or seven instances fit where two did.
- The launch page's action group sits above the footer, so a taller window moves it down instead of leaving a gap.
- The detail page's back control names where it returns to — 返回启动页 when you came from the launch page, 返回实例列表 when you came from the grid.
- 「修改」 can now download a version the instance does not have yet and switch to it, instead of only repointing between installed ones.
- Fields and buttons take their height from the font and the shipped padding. Text no longer clips on machines whose font is a little taller than the one the layout was drawn against.
- The default theme follows the operating system on a fresh install.
- CurseForge: `MCLAUNCHER_CF_KEY` overrides the configured key, the source is hidden entirely when no key is set, and requests are bounded to 16 concurrent with three retries on transient errors.
- The game process runs on its own thread; the font list and the instance scan left the GUI thread; exit no longer builds the About page.

## 🐛 Fixes

- Extracting the natives archive rejects entries that would escape the target folder.
- Update packages are verified against the digest published with the release; the signature check is only a fallback when no digest is declared.
- Switching to the instances page no longer flashes two empty half-transparent windows.
- Account avatars show the account's skin face; the letter is only a fallback.
- Timers no longer call into widgets that were already deleted, which used to happen when a language switch rebuilt the pages.

---

## ✨ 新增

- **内置联机**。新增「工具」页，其「联机」标签封装了陶瓦联机：在已经对局域网开放的世界里创建大厅，或用大厅编号加入。卡片显示编号、成员列表和连接时的打洞难度。**关掉启动器房间依然保留**——标题栏的小胶囊能带你回到它，重启之后也能接上。
- 同一标签里有**网络自检**，给出 UDP 与 TCP 的 NAT 类型，在动手之前就能判断“我该不该当房主”。
- **整合包导入**：支持 Modrinth `.mrpack`、CurseForge zip、MultiMC/Prism 实例。选一个压缩包，启动器装好版本、加载器和文件，再建一个指向它们的实例。
- **整合包导出**：实例可以写成标准的 `.mrpack` 或 CurseForge 整合包，别的启动器能直接导入。
- **崩溃分析重做**：三十条成因、从堆栈里指认模组、给出置信度、带文件名与行号的证据，以及可执行的动作（禁用这个模组、打开 mods 文件夹、调整内存）。游戏崩溃时以窗口内卡片的形式呈现。
- **启动器自动更新**：后台检查、下载、核对发布摘要、就地替换可执行文件并重启；替换失败会回滚。
- **投影浏览器**：实例的 `.litematic` 文件有了独立标签，显示尺寸、方块种类和文件元数据。
- **截图与服务器列表**成了实例里的两个标签：截图同时读取实例目录与共享目录，服务器列表以原子方式读写 `servers.dat`。

## 🔧 改进

- 实例列表改用 88 像素紧凑行，原来一屏两张卡，现在能放六七条。
- 启动页的动作组贴在页脚上方，窗口拉高时跟着下移，不再留空档。
- 详情页的返回按钮会说明回到哪里——从启动页进来是「返回启动页」，从列表进来是「返回实例列表」。
- 「修改」可以直接下载一个还没装过的版本并切过去，不再只能在已安装的版本之间改指向。
- 输入框和按钮的高度改由字体与内边距决定。字体比设计时高一点的机器上，文字不会再被切掉。
- 新装默认跟随系统深浅色。
- CurseForge：`MCLAUNCHER_CF_KEY` 可覆盖配置里的 key；没有 key 时该来源整个不显示；请求限制 16 并发并对暂时性错误重试三次。
- 游戏进程改为独立线程；字体列表与实例扫描移出 GUI 线程；退出不再构建关于页。

## 🐛 修复

- 解压 natives 时拒绝会跑到目标目录之外的条目。
- 更新包改为核对随发布一起提供的摘要，签名检查只在没有摘要时作为兜底。
- 切到实例页时不再闪出两个空的半透明窗口。
- 账号头像显示皮肤脸，字母只作为兜底。
- 定时器不再调用已经被销毁的控件——切换语言重建页面时原本会发生。
