# AGENTS.md

Working notes for AI coding agents (and humans who prefer a checklist) in this repository.
`CONTRIBUTING.md` is the human-facing guide; this file is the operating manual: what to run, what not to
touch, and the traps this codebase has already paid for.

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

Run from the repository root, with the virtual environment of your choice activated:

```powershell
python -m gui                            # run the app (a visible window)
python -m ruff check launcher gui tests  # lint, always
python tools/ui_regression.py --list-checks   # the gate's current checks - the only source of truth
python tools/ui_regression.py --quick         # the gate (skips screenshot rendering)
python tools/ui_regression.py                 # the gate + screenshots
python -m pytest -q                           # the whole suite alone, ~22 minutes
python scripts/check_headers.py --fix         # GPL header on a new source file
python tools/check_readme_sync.py             # nine READMEs must report 0 drift
python tools/build_source_zip.py              # writes dist/Source_code.zip; the release copies it to Releases/
python -m PyInstaller build_exe.spec --noconfirm
powershell -ExecutionPolicy Bypass -File scripts/sign_exe.ps1
```

The check list is **never** written down here or in any other document: it lives in
`tools/ui_regression.py` and is printed by `--list-checks`. The only hard requirement is that the command
outputs all OK. `scripts/sign_exe.ps1` signs with the project's code-signing certificate from the current
user's certificate store (subject `CN=WDNDXLTX`); without it the script throws and
`dist/MinePick_Launcher.exe` stays unsigned.

## Test environment (this is where runs go wrong)

- **Always** set `QT_QPA_PLATFORM=offscreen` and clear `MCLAUNCHER_DATA_DIR` before `pytest`.
- The suite is offline by default: autouse fixtures in `tests/conftest.py` cut the HTTP cache, the
  Modrinth identity lookup, the update check and every modal dialog. A test that needs a response mocks
  it with `respx`; nothing may reach the network.
- `tmp_path` is **not writable** in a sandboxed runner - use the `ws_tmp` fixture.
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
- Each language's name is written in its own script (the language picker shows `English (US)` for
  English), never translated along with the UI. The font fallback chain must cover CJK; a missing
  Korean font paints whole pages as empty boxes.
- Legal and compliance wording must not weaken in any language: a statement like "this project is an
  independent implementation and contains no upstream source code" has to stay equivalent in all nine,
  neither omitted nor softened.
- UI labels are in the interface language (Chinese by default); identifiers, comments, docs and
  commits are English. When searching the code, search the English identifier, not the label. A
  code comment may quote the Chinese label it talks about, because that is the string a reader sees
  on screen; the sentence around the quote stays English. This guide itself stays free of them.
- `docs/glossary.md` maps each label those comments quote to its English term, with the
  `gui/i18n.py` key beside it; tests/test_glossary.py keeps it matching the language tables.

## Reading someone else's source code

Borrowing ideas from another launcher is allowed; copying from it is not. All five rules apply together,
and missing one means the work does not start:

1. **Read only.** Understand the approach and information architecture; copy no code, text or assets. The
   implementation and the wording are always written here.
2. **Outside the repository.** A reference checkout lives in its own folder outside the project - never
   inside the working tree, the source archive or `build_source_zip.py`'s list.
3. **On the record.** Note which repository and module was consulted, and credit it in the About page with
   the agreed wording - the "no upstream source code" statement must not weaken (§ Conventions).
4. **Prove it before a release.** Count `.cs`, `.xaml`, `.sln`, `.csproj`, `.vbproj` files inside the
   repository and the source archive: **0**.
5. **Never a dependency.** Reference code is not imported, not copied into `tools/` or `launcher/`; an
   algorithm worth borrowing is rewritten in this project's own language and structure.

## Traps and invariants

The Qt traps this codebase has already paid for, and the product invariants that are easy to break,
live in `CONTRIBUTING.md` (§ Qt traps already paid for, § Product invariants). Read them before
touching the GUI or the launcher core: they are written for a human reader, which is the same answer
an agent needs.

## Release path (do not start it on your own)

Version bumps, the `vX.Y.Z` tag, `Releases/` artifacts, the README banners and the push happen only when
the maintainer says so. The parts of the checklist that are easiest to forget:

- version identical in the code, `pyproject.toml` and all nine READMEs;
- the slow tests run once with `MINE_PICK_SLOW_TESTS=1` (they are skipped otherwise);
- the EXE rebuilt, signature `Valid`, smoke test exit 0 - the smoke run is a **separate, elevated** step,
  not part of the build batch;
- `Releases/MinePick_Launcher.exe` and `Releases/Source_code.zip` (copied from `dist/`) opened and checked:
  version inside the package, nine READMEs, screenshots present, no `tests/.work` leftovers, per-file
  SHA-256, and the change you just made actually present;
- screenshots re-rendered for the pages a UI change touched (`zh` and `en`);
- `python tools/check_readme_sync.py` reports 0 drift, every new string exists in all nine languages;
- release notes bilingual, empty sections dropped, user-visible changes only;
- working tree clean and the tag pointing at the newest commit.
