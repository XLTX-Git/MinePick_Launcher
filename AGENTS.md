# AGENTS.md

Working notes for AI coding agents (and humans who prefer a checklist) in this repository.
`CONTRIBUTING.md` is the human-facing guide; this file is the operating manual: what to run, what not to
touch, and the traps this codebase has already paid for.

## What this project is

A portable Minecraft launcher: Python 3.11+, PySide6 GUI, single-file PyInstaller EXE, GPL-3.0-only.
The user-facing UI is **Chinese-first** and ships in nine languages. Everything is offline-first: the
launcher reads/writes its own data directory next to the EXE (`config/`), never the registry.

| Area | Where |
|---|---|
| Backend (no Qt) | `launcher/` — install, launch, instances, mods, java, link (Terracotta), net, meta, update |
| GUI | `gui/` — `main_window.py`, `pages/`, `widgets.py`, `theme.py`, `i18n.py`, `tasks.py` |
| Stylesheets | `gui/resources/style.qss` (dark), `style_light.qss` |
| Tests | `tests/` (~2 180 tests, ~22 minutes for the full run) |
| Verification tools | `tools/` — `ui_regression.py`, `audit_*.py`, `check_readme_sync.py`, `shot_pages.py`, `build_source_zip.py` |
| Docs | `docs/` — `<name>.md` is **English**, `<name>_zh.md` is **Chinese** (same direction as the README) |
| Packaging | `build_exe.spec` → `dist/MinePick_Launcher.exe`, `scripts/sign_exe.ps1` |

## Commands

```powershell
pip install -r requirements-dev.txt      # once
python -m gui                            # run the app (a visible window)
ruff check launcher gui tests            # lint, always
python tools/ui_regression.py --quick    # release gate: unit tests + lint + headers + readme sync + UI quality + corner audits
pytest -q                                # the full suite alone, ~22 minutes
python scripts/check_headers.py --fix    # GPL header on a new source file
python tools/check_readme_sync.py        # nine READMEs must report 0 drift
python tools/build_source_zip.py         # writes dist/Source_code.zip; the release copies it to Releases/
pyinstaller build_exe.spec --noconfirm && powershell -ExecutionPolicy Bypass -File scripts/sign_exe.ps1
```

`scripts/sign_exe.ps1` finds a code-signing certificate in `Cert:\CurrentUser\My` whose subject is
`CN=WDNDXLTX`; without it the script throws, and `dist/MinePick_Launcher.exe` stays unsigned.

## Test environment (this is where runs go wrong)

- **Always** `$env:QT_QPA_PLATFORM = "offscreen"` and **unset** `MCLAUNCHER_DATA_DIR` before `pytest`.
- The suite is offline by default: autouse fixtures in `tests/conftest.py` cut the HTTP cache, the
  Modrinth identity lookup, the update check and every modal dialog. A test that needs a response mocks
  it with `respx`; nothing may reach the network.
- `tmp_path` is **not writable** in this sandbox — use the `ws_tmp` fixture.
- A test that puts a real window on the desktop fails the run (autouse guard). Use the `app` fixture and
  `WA_DontShowOnScreen`.
- Known flake (GUI worker thread native crash, reproducible on a clean commit): rerun the whole suite
  once; if it still fails, split in half (`-k "not java and not locate and not detected"` and its
  complement). Both halves green → flake, append `(flake rerun passed)` to the commit message. Either
  half crashing on its own rerun → real failure, do not commit.
- Re-pinned digests are not cheating: `tests/test_dark_state_contrast.py` and
  `tests/test_dark_surface_layers.py` hold the light sheet's SHA-256, and `tools/audit_corners.py` holds
  a list of accepted 1-pixel corner blends keyed by **widget kind, corners and size**. When a stylesheet
  or a widget height changes, update the digest/list with the reason, never delete the check.

## Conventions

- Every new source file starts with the 14-line GPL header (copy one from `launcher/repair.py`). A file
  without it fails `tests/test_headers.py`.
- Commit messages: **English only**, conventional prefix (`feat`/`fix`/`docs`/`test`/`chore`/`refactor`),
  imperative subject ≤72 chars, body says what and **why** plus the commands you ran. One logical change
  per commit.
- User-visible strings are never hardcoded. UI text: `gui/i18n.py` + `gui/i18n_langs.py`. Core text:
  `launcher/i18n.py` + `launcher/i18n_langs.py`. Nine locales: `zh_cn`, `zh_tw`, `en_us`, `ja_jp`,
  `ko_kr`, `ru_ru`, `fr_fr`, `es_es`, `de_de`. New key → all nine tables, or the i18n tests fail.
- Chinese UI terms used as identifiers in prose: 实例 = instance, 整合包 = modpack, 联机 = multiplayer
  (Terracotta), 投影原理图 = schematics, 修改 = modify, 导出 = export/backup.

## Qt traps already paid for (do not relearn them)

- **Stylesheets apply to a window, not the application.** A `QComboBox` popup is a top-level window of
  its own, so `gui/popup_shape.py` styles it explicitly and paints its rows with a delegate — the platform
  delegate ignores `::item` padding, which is why `padding` and `height` in the sheet do nothing there.
- **`QStackedWidget.sizeHint()` is the maximum over its pages.** The instance detail page therefore drives
  its own scroll geometry (`_FitStack` + `_sync_content_geometry`), and a page's height comes from
  `layout.heightForWidth(width)` when the layout supports it — `sizeHint()` answers for an ideal width
  and over-reports word-wrapped cards.
- **Never put a `QPushButton` inside another button's layout.** The inner one inherits the outer height and
  draws its text in a squeezed content rect (this clipped 「测速」 down to a few pixels).
- **`clicked` carries a bool.** `lambda group=key: ...` silently receives `False`; write
  `lambda *_, group=key: ...`.
- **Sizing from a widget's `sizeHint()` before it is shown returns 0 or a stale value.** `LoaderVersionItem`
  returns its constant height for that reason.
- **`Path.replace` fails with `PermissionError` while another thread reads the target.** The mirror report
  retries; keep that.
- Window floor is **1000×610**; click targets ≥24×24 px; both themes (dark/light) × eight accents are
  audited for contrast, and `tools/ui_regression.py` is the gate.

## Product rules that are easy to break

- Icons for mods/resource packs come **only** from hash/fingerprint matches (Modrinth sha1, CurseForge
  MurmurHash2). Never guess an icon from a name.
- Terracotta (AGPL-3.0 with an exception) and EasyTier (LGPL-3.0) ship as unmodified binaries under
  `components/`. The footer credit 「联机由 Terracotta | 陶瓦联机 提供」 is mandatory, and
  `components/easytier` must never be deleted by code that "disables" multiplayer — it powers the
  network self-check.
- 关闭大厅 means the tunnel stops and guests drop; a core this launcher started is also terminated, a
  borrowed one is left alone.
- Downloads are tasks (`gui/tasks.py`): a new long-running download needs `TaskCenter.add_task` with a
  cancel path, not just a status line.
- An installed Java runtime must actually start (`java -version`) or it is removed and reported; never
  hand back a runtime you have not probed.

## Release path (do not start it on your own)

Version bumps, the `vX.Y.Z` tag, `Releases/` artifacts, the README banners and the push happen only when
the maintainer says so. The gate is `python tools/ui_regression.py --quick` printing all checks OK, plus an
out-of-box check of `Releases/MinePick_Launcher.exe` and `Releases/Source_code.zip` - version inside the
package, nine READMEs, no `tests/.work` leftovers, per-file SHA-256.
