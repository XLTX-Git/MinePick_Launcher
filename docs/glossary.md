# UI glossary: the Chinese labels that code comments quote

The interface ships in nine languages and is Chinese-first, so a backend or GUI comment often
quotes the label a person sees on screen (`# 截图 / 服务器 are instance data`). This table is what
those quotes mean in English. Every row names the `gui/i18n.py` key that produces the text, so the
mapping is checked by a test instead of trusted.

The first column is the form comments actually use - sometimes a shorthand of the full label
(`投影` for `投影原理图`). Labels that are templates (`{}` inside) are left out: a comment quotes the
fixed part, and the numbers are the caller's.

## Main navigation

| Chinese | English | Key |
|---|---|---|
| 启动 | Launch | `nav.launch` |
| 实例 | Instances | `nav.instances` |
| 版本 | Versions | `nav.versions` |
| Java | Java | `nav.java` |
| 账号 | Account | `nav.account` |
| 资源 | Resources | `nav.mods` |
| 工具 | Tools | `nav.tools` |
| 设置 | Settings | `nav.settings` |
| 关于 | About | `nav.about` |

## Instance detail sidebar

| Chinese | English | Key |
|---|---|---|
| 概览 | Overview | `instances.detail.nav.overview` |
| 模组 | Mods | `instances.detail.nav.mods` |
| 资源包 | Resource packs | `instances.detail.nav.resourcepacks` |
| 光影包 | Shaders | `instances.detail.nav.shaderpacks` |
| 投影 | Schematics | `instances.detail.card.schematic` |
| 存档 | Saves | `instances.detail.nav.saves` |
| 截图 | Screenshots | `nav.screenshots` |
| 服务器 | Servers | `nav.servers` |
| 修改 | Change | `instances.detail.nav.modify` |
| 导出 | Export | `instances.detail.nav.export` |
| 诊断 | Diagnosis | `instances.detail.nav.diagnosis` |

## Actions

| Chinese | English | Key |
|---|---|---|
| 使用 | Use | `instances.card.use` |
| 管理 | Manage | `instances.card.manage` |
| 安装 | Install | `versions.install.short` |
| 取消 | Cancel | `quit.busy.cancel` |
| 全选 | Select all | `instances.detail.export.files.select_all` |
| 全不选 | Select none | `instances.detail.export.files.select_none` |
| 开始导出 | Start export | `instances.detail.export.start` |
| 导出为整合包 | Export as modpack | `instances.detail.export.pack.button` |
| 打包资源文件 | Pack the resource files | `instances.detail.export.embed` |
| 开始重置 | Reset the core | `instances.detail.modify.reset.start` |
| 新建/复制实例 | New / Copy Instance | `instances.create_copy` |
| 导入 | Import | `instances.import_menu` |
| 添加文件夹 | Add folder… | `settings.folder.add` |
| 设为当前 | Set current | `settings.folder.make_current` |
| 打开实例文件夹 | Open Instance Folder | `instances.open_folder` |
| 打开日志文件夹 | Open Logs Folder | `instances.detail.open_logs` |
| 查看崩溃报告 | View Crash Reports | `crash.viewer` |
| 刷新列表 | Refresh | `versions.refresh` |
| 停用联机功能 | Disable multiplayer | `tools.footer.disable` |

## States and labels

| Chinese | English | Key |
|---|---|---|
| 当前 | Current | `account.status.current` |
| 已取消 | Cancelled | `tasks.state.cancelled` |
| 失败 | Failed | `tasks.state.failed` |
| 超时 | Timed out | `tools.onboard.channels.timeout` |
| 未测 | Not measured | `tools.onboard.channels.unmeasured` |
| 联机中 | Multiplayer | `link.badge.active_no_code` |
| 正式版 | Release | `versions.type.release` |
| 快照版 | Snapshot | `versions.type.snapshot` |
| 愚人节版 | April Fools | `versions.group.april` |
| 远古版 | Legacy | `versions.filter.legacy` |
| 带加载器 | With a loader | `instances.group.loader` |
| 其他文件夹 | Other folders | `instances.detail.export.category.other` |

## How a comment should quote a label

Quote the exact label, or the shortest form that still matches it, and keep the sentence English:

```python
# 截图 / 服务器 are instance data (the game writes both into launch_dir), so they sit with the
# other game resources rather than in a third group.
```

Do not translate the label inside the quote: the reader can look it up here, and a translated
quote would no longer be findable in the interface or in the language tables.
