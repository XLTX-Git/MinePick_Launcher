# -*- mode: python ; coding: utf-8 -*-
# MinePick Launcher PyInstaller 打包：只产出 GUI 版
#   MinePick_Launcher.exe —— GUI（无控制台窗口，双击启动）
# 本仓库不提供 CLI 版，因此不构建 MinePick_Launcher_cli.exe
# 用法: pyinstaller build_exe.spec
# 前置: pip install .[gui] pyinstaller

from pathlib import Path

block_cipher = None

# CurseForge 内置 Key 是可选的：build/cf_key.txt 被 gitignore，干净克隆里没有它。
# 有就随包打进 launcher/mods/data/；没有也必须能打包——运行时 key 为空，
# launcher/mods/curseforge.py 的 available() 会让资源页直接不提供 CurseForge 来源。
_cf_key = Path(SPECPATH) / "build" / "cf_key.txt"
_cf_key_datas = [(str(_cf_key), "launcher/mods/data")] if _cf_key.is_file() else []

a = Analysis(
    ["run.py"],
    pathex=[],
    binaries=[],
    # 随包资源：GUI 图标/QSS（运行时经 launcher.paths.resource_path() 读取）
    datas=[
        ("gui/resources", "gui/resources"),
        ("launcher/mods/data", "launcher/mods/data"),
        # 崩溃/启动失败知识库（launcher/diagnostics/rules.py 经 resource_path() 读取）
        ("launcher/diagnostics/data", "launcher/diagnostics/data"),
        # 第三方许可声明随包携带（关于页展示的同一份清单，仓库里由工具生成）
        ("THIRD_PARTY_NOTICES.md", "."),
        # CurseForge 内置 Key：来自 gitignored 的 build/cf_key.txt，不进入源码仓库
        *_cf_key_datas,
    ],
    hiddenimports=[
        # pydantic 动态导入与常用可选模块
        "pydantic",
        "pydantic.deprecated.decorator",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter"],
    noarchive=False,
)

def _windows_version_file() -> str:
    """Write PyInstaller's version resource from ``launcher.__version__`` and return its path.

    The EXE properties used to show no version at all: the spec never passed ``version=``, so Windows
    fell back to an empty resource. Reading the constant out of the source keeps the file honest
    without importing the package into the build.
    """
    import re
    from pathlib import Path

    root = Path(SPECPATH)
    source = (root / "launcher" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*"([^"]+)"', source, re.MULTILINE)
    version = match.group(1) if match else "0.0.0"
    numbers = (version.split(".") + ["0", "0", "0"])[:4]
    tuple_text = ", ".join(str(int(part)) if part.isdigit() else "0" for part in numbers)
    body = f"""# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(filevers=({tuple_text}), prodvers=({tuple_text}), mask=0x3f, flags=0x0,
                    OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('080404b0', [
      StringStruct('FileVersion', '{version}'),
      StringStruct('ProductVersion', '{version}'),
      StringStruct('FileDescription', 'MinePick Launcher'),
      StringStruct('ProductName', 'MinePick Launcher'),
      StringStruct('OriginalFilename', 'MinePick_Launcher.exe'),
      StringStruct('LegalCopyright', 'GPL-3.0-only')])]),
    VarFileInfo([VarStruct('Translation', [2052, 1200])])
  ]
)
"""
    target = root / "build" / "version_info.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    return str(target)


pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="MinePick_Launcher",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon="gui/resources/icon.ico",  # 由 icon.svg 渲染生成（钻石镐+齿轮）
    version=_windows_version_file(),
)
