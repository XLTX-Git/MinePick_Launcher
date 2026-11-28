# Contributing to MinePick Launcher

MinePick Launcher is a portable Minecraft launcher built with Python and PySide6. Contributions are
welcome — code, tests, documentation, translations and bug reports alike. This file describes how the
project actually works, so that a change can be reviewed, tested and released without a second round of
questions.

Chinese version: [CONTRIBUTING_zh.md](CONTRIBUTING_zh.md)

## Before you start

- Read [README.md](README.md) and the documents under [`docs/`](docs/) first.
- The project is licensed **GPL-3.0-only**. By contributing you agree that your work is distributed
  under the same licence.
- Keep discussion in issues and pull requests. Harassment, personal attacks and disclosure of another
  person's data are not tolerated.
- **Do not open a public issue for a security problem** — see [Security](#security).

## Set up

Windows 10 / 11 (64-bit) and Python 3.11 or newer are required.

```powershell
git clone https://github.com/XLTX-Git/MinePick_Launcher.git
cd MinePick_Launcher
pip install -r requirements-dev.txt
python -m gui
```

The test suite runs headless. Set the platform plugin before running it, or the GUI tests will try to
put windows on your desktop:

```powershell
$env:QT_QPA_PLATFORM = "offscreen"
```

## The three commands

How much of this you run depends on what you changed:

```powershell
ruff check launcher gui tests            # lint, every change
python tools/ui_regression.py --quick    # any UI change; runs seven checks
pytest -q                                # the whole suite, ~20 minutes
```

- **Documentation only** (README, docs, CONTRIBUTING): no `pytest` at all. Run
  `python tools/check_readme_sync.py` and the README tests
  (`pytest tests/test_readme_page_header_claim.py`) — that is what those files can break.
- **While developing**: run the test files you touched. A single file is seconds, and that is the loop
  you want.
- **Before marking a pull request ready**: the whole suite once, `pytest -q` (~20 minutes). The lint and
  UI checks above apply to every change either way.

`--quick` skips screenshots. The release gate runs the full script, so a UI change that only passes
`--quick` is not finished — but it is enough to open a pull request with.

## Tests

- **The suite is offline by default.** Autouse fixtures in `tests/conftest.py` cut the HTTP cache, the
  Modrinth identity lookup, the update check and every modal dialog. A test that needs a network answer
  must mock it (`respx` is in the dev requirements).
- `tmp_path` is not writable in this environment; use the `ws_tmp` fixture when a test needs files.
- GUI tests use the `app` fixture. Never show a real window: an autouse guard fails the run if one reaches
  the desktop.
- Pin what a user can observe, not how it is implemented. When a deliberate change breaks a pinned test,
  update the test **and** explain the new behaviour in its docstring — in the same commit.
- Known flake: the GUI worker thread occasionally crashes natively, and a clean commit reproduces it.
  Rerun the whole suite once; if it still fails, split it in half
  (`-k "not java and not locate and not detected"` and its complement). Both halves green → treat it as
  the flake and add `(flake rerun passed)` to the commit message. Either half crashing on its own rerun →
  a real failure, do not commit.

## Code

- Lint and formatting settings live in `pyproject.toml`; run `ruff check` and let it decide.
- **Every new source file starts with the GPL header block** — `SPDX-FileCopyrightText`,
  `SPDX-License-Identifier: GPL-3.0-only`, the "This file is part of MinePick Launcher." sentence and the
  full grant. Copy it from an existing module; a file without it fails the regression gate.
- Type hints on public functions. Comments explain *why* something is the way it is, not what the next
  line does.
- A new dependency needs a licence check (GPL-compatible only) and a
  `python tools/build_third_party_notices.py` run, so `THIRD_PARTY_NOTICES.md` stays in step.
- The interface is Chinese-first, so a comment may quote the label it talks about; [`docs/glossary.md`](docs/glossary.md) is the map from those labels to English.

## Strings and translations

- No user-visible string is hardcoded. UI text goes through `gui/i18n.py` + `gui/i18n_langs.py`; core
  (non-GUI) text through `launcher/i18n.py` + `launcher/i18n_langs.py`.
- The interface ships in nine languages: `zh_cn`, `zh_tw`, `en_us`, `ja_jp`, `ko_kr`, `ru_ru`, `fr_fr`,
  `es_es`, `de_de`. A new key needs an entry in all nine tables. A missing translation falls back to
  English at runtime, which is a bug, not a shortcut.
- The README exists in nine languages. `python tools/check_readme_sync.py` must report `0 drift`, and
  `pytest tests/test_readme_page_header_claim.py` pins that no README promises UI that no longer exists.
- Technical documents come in pairs, named the same way the README is: `docs/x.md` is English,
  `docs/x_zh.md` is Chinese. Update both.

## UI changes

- Click targets are at least 24×24 px; every interactive control carries an accessible name; keyboard
  focus is always visible. Colours come from theme tokens, and both themes × eight accent presets are
  audited for contrast.
- The window floor is 1000×610. A page may not gain a scrollbar it did not need, and a tab may not be
  scrollable past its own content.
- Regenerate screenshots when a page changes: `python tools/shot_pages.py --root . --preview .
  --out docs/screenshots --language zh_cn`, then the same with `--language en_us` (the script needs
  `--root` and `--out`; a bare `python tools/shot_pages.py` exits with an argparse error). Only `zh`
  and `en` captures exist, so a translated README notes which interface its screenshots show.

## Commits and pull requests

- Commit messages are **English only** — a mixed-language message is rejected in review. Machine
  translation is fine: `fix: resolve the launcher crash on startup` is right, `fix: <the same sentence in Chinese>` is not.
- Use a conventional prefix: `feat`, `fix`, `docs`, `test`, `chore`, `refactor`, `perf`, `style`.
- Subject in the imperative mood, at most 72 characters. The body says what changed and **why**, and
  names the commands you ran (for example "pytest tests/test_tools_page.py: 63 passed").
- One logical change per commit. A commit that fixes two unrelated things should be two commits.

## Releases

- Version numbers are bumped only when a release is made — do not leave an unreleased version in the
  tree. The rule and the bump table are in [`docs/github_release.md`](docs/github_release.md).
- The release gate is `python tools/ui_regression.py --quick` printing all checks OK, plus an out-of-box
  check of the packaged EXE and `Source_code.zip` (version inside the package, nine READMEs, no
  `tests/.work` leftovers, per-file SHA-256).

## Reporting bugs

Include the launcher version (from the About page), your Windows build, what you did, what happened, and
the diagnosis export from the Diagnostics page — it redacts access tokens and user paths. Launcher-side failures
carry an `MPL-Launch-####` code, game crashes `MPL-Crash-####`; the code alone often identifies the cause.

## Security

Do not open a public issue for a vulnerability. Use GitHub's private *Report a vulnerability*
(Security → Advisories) or contact the maintainer directly. We will credit you unless you ask us not to.

## Third-party components

Terracotta (AGPL-3.0 with an exception) and EasyTier (LGPL-3.0) are shipped as unmodified binaries under
`components/`. Their licence texts, the source offer and the required UI credit
(the Terracotta credit line in the footer - the exact text lives in `launcher/notices.py`) are not optional, and `components/easytier` must not be deleted by
code paths that "disable" the multiplayer feature — it powers the network self-check.
