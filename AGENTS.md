# AGENTS.md

Working notes for AI coding agents (and humans who prefer a checklist) in this repository.
`CONTRIBUTING.md` is the human-facing guide; this file is the operating manual: what to run, what not to
touch, and the traps this codebase has already paid for.

The project's process manual is **`D:\dsh-workspace\MinePick_Docs\MinePick_开发标准流程.md`** (Chinese,
outside this repository). It is authoritative for anything about *process* - routine, packaging,
versioning, release, the full list of known pitfalls. Read it before a release or when this file does not
answer a question. When you discover a new trap, it goes into **§8 已知坑** with one line in **§11 变更
记录** in the same round - the SOP's §10 requires that, and a trap that is not written down will be
stepped on again.

## Scope discipline

Do what was asked. Do not add "related improvements" nobody requested - no extra refactors, no unasked
features, no opportunistic renames. The one exception is the step without which the request cannot be
completed at all: change a UI element and the screenshots that show it are part of the change, otherwise
the deliverable contradicts itself.

## What this project is

A portable Minecraft launcher: Python 3.11+, PySide6 GUI, single-file PyInstaller EXE, GPL-3.0-only.
The user-facing UI is **Chinese-first** and ships in nine languages. Everything is offline-first: the
launcher reads/writes its own data directory next to the EXE (`config/`), never the registry.

| Area | Where |
|---|---|
| Backend (no Qt) | `launcher/` - install, launch, instances, mods, java, link (Terracotta), net, meta, update |
| GUI | `gui/` - `main_window.py`, `pages/`, `widgets.py`, `theme.py`, `i18n.py`, `tasks.py` |
| Stylesheets | `gui/resources/style.qss` (dark), `style_light.qss` |
| Tests | `tests/` (~2 180 tests, ~22 minutes for the full run) |
| Verification tools | `tools/` - `ui_regression.py`, `audit_*.py`, `check_readme_sync.py`, `shot_pages.py`, `build_source_zip.py` |
| Docs | `docs/` - `<name>.md` is **English**, `<name>_zh.md` is **Chinese** (same direction as the README) |
| Screenshots | `docs/screenshots/<page>_zh.png` and `_en.png`; the seven other READMEs embed `_en`, the two Chinese ones `_zh` |
| Packaging | `build_exe.spec` → `dist/MinePick_Launcher.exe`, `scripts/sign_exe.ps1` |

## Commands

```powershell
$py = "D:\dsh-workspace\minecraft-launcher\.venv\Scripts\python.exe"
cd D:\dsh-workspace\Source_code_UI
& $py -m gui                              # run the app (a visible window)
& $py -m ruff check launcher gui tests    # lint, always
& $py tools/ui_regression.py --list-checks      # the gate's current checks - the only source of truth
& $py tools/ui_regression.py --quick            # the gate (skips screenshot rendering)
& $py tools/ui_regression.py                    # the gate + screenshots
& $py -m pytest -q                              # the whole suite alone, ~22 minutes
& $py scripts/check_headers.py --fix            # GPL header on a new source file
& $py tools/check_readme_sync.py                # nine READMEs must report 0 drift
& $py tools/build_source_zip.py                 # writes dist/Source_code.zip; the release copies it to Releases/
& $py -m PyInstaller build_exe.spec --noconfirm
powershell -ExecutionPolicy Bypass -File scripts/sign_exe.ps1
```

The check list is **never** written down here or in any other document: it lives in
`tools/ui_regression.py` and is printed by `--list-checks`. The only hard requirement is that the command
outputs all OK. `scripts/sign_exe.ps1` finds a code-signing certificate in `Cert:\CurrentUser\My` whose
subject is `CN=WDNDXLTX`; without it the script throws and `dist/MinePick_Launcher.exe` stays unsigned.

## Test environment (this is where runs go wrong)

- **Always** `$env:QT_QPA_PLATFORM = "offscreen"` and **unset** `MCLAUNCHER_DATA_DIR` before `pytest`.
- The suite is offline by default: autouse fixtures in `tests/conftest.py` cut the HTTP cache, the
  Modrinth identity lookup, the update check and every modal dialog. A test that needs a response mocks
  it with `respx`; nothing may reach the network.
- `tmp_path` is **not writable** in this sandbox - use the `ws_tmp` fixture.
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
  per commit. A release commit is the bare version (`v0.5.0`) and nothing else.
- Scan `git status` before every commit. Temporary render output (`_prev/`, `_preview/`, `preview/`) and
  one-off scripts (`tools/_*.py`) are ignored by git but must not be staged, and must never end up in a
  release artifact.
- User-visible strings are never hardcoded. UI text: `gui/i18n.py` + `gui/i18n_langs.py`. Core text:
  `launcher/i18n.py` + `launcher/i18n_langs.py`. Nine locales: `zh_cn`, `zh_tw`, `en_us`, `ja_jp`,
  `ko_kr`, `ru_ru`, `fr_fr`, `es_es`, `de_de`. A new key needs all nine tables in the same batch; the
  key-set tests fail otherwise.
- **The runtime fallback is `en_us`**, in all four places that fall back (`tr()`, `tr_core()`,
  `set_language()`/`set_core_language()`, `detect_system_language()`). Never `zh_cn`. Falling back is a
  safety net for third-party data, not a licence to ship English placeholders and translate later.
- Language names are written in their own language (`English (US)`, `日本語`, `한국어`), never translated
  with the UI. The font fallback chain must cover CJK; a missing Korean font paints whole pages as tofu.
- Legal and compliance wording must not weaken in any language: a statement like "this project is an
  independent implementation and contains no upstream source code" has to stay equivalent in all nine,
  neither omitted nor softened.
- Chinese UI terms used as identifiers in prose: 实例 = instance, 整合包 = modpack, 联机 = multiplayer
  (Terracotta), 投影原理图 = schematics, 修改 = modify, 导出 = export/backup.

## Reading someone else's source code

Borrowing ideas from another launcher is allowed; copying from it is not. All five rules apply together,
and missing one means the work does not start:

1. **Read only.** Understand the approach and information architecture; copy no code, text or assets. The
   implementation and the wording are always written here.
2. **Outside the repository.** A reference checkout lives in its own folder at the workspace root (today
   `D:\dsh-workspace\PCL-CE-reference\`), never inside the project, the source archive or
   `build_source_zip.py`'s list.
3. **On the record.** Note which repository and module was consulted, and credit it in the About page with
   the agreed wording - the "no upstream source code" statement must not weaken (§ Conventions).
4. **Prove it before a release.** Count `.cs`, `.xaml`, `.sln`, `.csproj`, `.vbproj` files inside the
   repository and the source archive: **0**.
5. **Never a dependency.** Reference code is not imported, not copied into `tools/` or `launcher/`; an
   algorithm worth borrowing is rewritten in this project's own language and structure.

## Qt traps already paid for (do not relearn them)

- **Stylesheets apply to a window, not the application.** A `QComboBox` popup is a top-level window of
  its own, so `gui/popup_shape.py` styles it explicitly and paints its rows with a delegate - the platform
  delegate ignores `::item` padding, which is why `padding` and `height` in the sheet do nothing there.
- **`QStackedWidget.sizeHint()` is the maximum over its pages.** The instance detail page therefore drives
  its own scroll geometry (`_FitStack` + `_sync_content_geometry`), and a page's height comes from
  `layout.heightForWidth(width)` when the layout supports it - `sizeHint()` answers for an ideal width
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

The rest of the catalogue (encoding, tooling that fakes failures, packaging, docs drift, thread
lifetime, UI heuristics - hundreds of numbered entries) is SOP §8. Consult it before touching threads,
packaging or the stylesheet.

## Product rules that are easy to break

- Icons for mods/resource packs come **only** from hash/fingerprint matches (Modrinth sha1, CurseForge
  MurmurHash2). Never guess an icon from a name.
- Terracotta (AGPL-3.0 with an exception) and EasyTier (LGPL-3.0) ship as unmodified binaries under
  `components/`. The footer credit 「联机由 Terracotta | 陶瓦联机 提供」 is mandatory, and
  `components/easytier` must never be deleted by code that "disables" multiplayer - it powers the
  network self-check.
- 关闭大厅 means the tunnel stops and guests drop; a core this launcher started is also terminated, a
  borrowed one is left alone.
- Downloads are tasks (`gui/tasks.py`): a new long-running download needs `TaskCenter.add_task` with a
  cancel path, not just a status line.
- An installed Java runtime must actually start (`java -version`) or it is removed and reported; never
  hand back a runtime you have not probed.

## Release path (do not start it on your own)

Version bumps, the `vX.Y.Z` tag, `Releases/` artifacts, the README banners and the push happen only when
the maintainer says so. The release checklist is SOP §12; the parts that are easiest to forget:

- version identical in the code, `pyproject.toml` and all nine READMEs;
- the slow tests run once with `$env:MINE_PICK_SLOW_TESTS=1` (they are skipped otherwise);
- the EXE rebuilt, signature `Valid`, smoke test exit 0 - the smoke run is a **separate, elevated** step,
  not part of the build batch;
- `Releases/MinePick_Launcher.exe` and `Releases/Source_code.zip` (copied from `dist/`) opened and checked:
  version inside the package, nine READMEs, screenshots present, no `tests/.work` leftovers, per-file
  SHA-256, and the change you just made actually present;
- screenshots re-rendered for the pages a UI change touched (`zh` and `en`);
- `python tools/check_readme_sync.py` reports 0 drift, every new string exists in all nine languages;
- release notes bilingual, empty sections dropped, user-visible changes only;
- working tree clean, tag pointing at the newest commit, and the new pitfall of this round written into
  SOP §8/§11.
