# SPDX-FileCopyrightText: 2026 WDNDXLTX
# SPDX-License-Identifier: GPL-3.0-only
#
# This file is part of MinePick Launcher.
#
# MinePick Launcher is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, version 3 of the License.
#
# MinePick Launcher is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with MinePick Launcher. If not, see <https://www.gnu.org/licenses/>.

"""Settings page: game directory, Java, memory, concurrency, version isolation, game language, launcher language."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFontDatabase, QTextLayout, QTextOption
from PySide6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QComboBox,
    QCompleter,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from gui import i18n
from gui.theme import DEFAULT_ACCENTS, accent_focus_ring, normalize_hex, resolve_theme
from gui.widgets import (
    AccessibilityPreferenceGroup,
    NoWheelSpinBox,
    add_page_top_inset,
    set_app_status,
    style_form,
    style_page_layout,
)
from launcher import config, paths

tr = i18n.tr

# One-click accent colours offered next to the hex field
ACCENT_PRESETS = ("#35a06a", "#3b82f6", "#a855f7", "#e0703a", "#e05a5a", "#14b8a6", "#ec4899", "#f59e0b")

# PCL's settings metrics scaled to this window, tightened one more step: this window is 1000px
# wide, so the rows are a 32px control plus a 6px gutter (38px pitch), with a fixed 216px label
# column. The column grew from 176px so the longest German/French/Russian labels wrap to at most
# two lines and still fit the 32px row (measured: 30px for two lines at 12px) instead of pushing
# the row taller. The sheet carries the colours and the radius.
FIELD_HEIGHT = 32
FIELD_LABEL_WIDTH = 216
ROW_SPACING = 6


def _wrap_checkbox_text(checkbox: QCheckBox, text: str, width: int) -> str:
    """Insert the line breaks a QCheckBox cannot compute for itself.

    QCheckBox draws its label on one line, so a long sentence - the encryption row is
    ~900px in English - used to force the whole settings card wider than the viewport,
    which pushed every field's right edge off-screen. QCheckBox does honour explicit
    newlines (measured: its size hint grows in height and shrinks to the longest line),
    so the label is laid out here at the card's text width and the resulting lines are
    joined with ``\\n``. Languages whose sentence already fits are returned untouched.
    """
    if checkbox.fontMetrics().horizontalAdvance(text) <= width:
        return text
    layout = QTextLayout(text, checkbox.font())
    option = QTextOption()
    option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
    layout.setTextOption(option)
    layout.beginLayout()
    lines: list[str] = []
    while True:
        line = layout.createLine()
        if not line.isValid():
            break
        line.setLineWidth(width)
        start = line.textStart()
        lines.append(text[start : start + line.textLength()].rstrip())
    layout.endLayout()
    return "\n".join(line for line in lines if line)


# Pinned to the top of the font list when installed
COMMON_FONTS = (
    "Microsoft YaHei UI",
    "Microsoft YaHei",
    "SimSun",
    "SimHei",
    "DengXian",
    "Segoe UI",
    "Consolas",
    "Cascadia Mono",
)


class AccentPresetButton(QPushButton):
    """One-click accent swatch, with a keyboard focus cue.

    A widget-level stylesheet wins over the application sheet for every property it declares, and this
    button declares both the fill and the border - so the sheet's
    ``QPushButton[keyboardFocus="true"]:focus`` rule never applied to it. Measured: focusing one of these
    changed 0 pixels, while the same rule on a plain button changed 28345. The ring therefore lives in
    this button's own sheet: the same 1px border, recoloured when focus is held. Nothing else differs, so
    the unfocused rendering is byte-for-byte what it was.
    """

    REST_BORDER = "#3a4757"

    def __init__(self, color: str, focus_ring: str) -> None:
        super().__init__()
        self._color = color
        self._focus_ring = focus_ring
        self.setFixedSize(22, 22)
        self.setToolTip(color)
        # WCAG 4.1.2: the button has no text at all - the colour is painted, not written - so the
        # tooltip was its only name and a hex value is not a name. It carries no visible label by
        # design (22x22 swatch), which is exactly why the accessible name has to say what it is.
        self.setAccessibleName(tr("a11y.accent_preset", color))
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._paint(self.REST_BORDER)

    def _paint(self, border: str) -> None:
        self.setStyleSheet(
            f"background: {self._color}; border: 1px solid {border}; border-radius: 5px;"
        )

    def focusInEvent(self, event) -> None:
        super().focusInEvent(event)
        self._paint(self._focus_ring)

    def focusOutEvent(self, event) -> None:
        super().focusOutEvent(event)
        self._paint(self.REST_BORDER)


class SettingsPage(QWidget):
    settings_changed = Signal()

    def __init__(self) -> None:
        super().__init__()
        # Scopes the PCL-style sheet rules to this page only (see style_light.qss). The attribute is
        # what lets a plain QWidget paint the gradient that rule names; without it Qt skips the
        # background and the page stays the window's white - i.e. the flat look this mock-up removes.
        self.setObjectName("settingsPage")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.game_dir_edit = QLineEdit()
        self.game_dir_edit.setPlaceholderText(tr("wizard.default_dir.hint"))  # 留空使用默认目录
        self.browse_game = QPushButton(tr("settings.browse"))
        self.browse_game.setObjectName("secondaryButton")
        self.java_path_edit = QLineEdit()
        self.java_path_edit.setAccessibleName(tr("settings.java"))  # WCAG 4.1.2
        self.browse_java = QPushButton(tr("settings.browse"))
        self.browse_java.setObjectName("secondaryButton")
        # Memory: a slider plus a live read-out (PCL's shape). The slider is the control; the
        # number is text, so a value stored outside the device range stays visible instead of
        # being silently rounded into the slider's range.
        self.memory_slider = QSlider(Qt.Orientation.Horizontal)
        self.memory_slider.setObjectName("memorySlider")
        self.memory_slider.setAccessibleName(tr("settings.memory"))
        self.memory_slider.setSingleStep(1)
        self.memory_slider.setPageStep(2)
        # The handle is a 16px circle: give the widget room for it instead of letting the
        # default 15px size hint clip it into an oval.
        self.memory_slider.setFixedHeight(20)
        self.memory_value_label = QLabel("")
        self.memory_value_label.setObjectName("memoryValue")
        self.memory_recommend_label = QLabel("")
        self.memory_recommend_label.setObjectName("hint")
        self.memory_warning_label = QLabel("")
        self.memory_warning_label.setObjectName("hint")
        self.memory_warning_label.setWordWrap(True)
        self.memory_warning_label.setVisible(False)
        self._memory_value = 4.0
        self._memory_min_gb = 1.0
        self._memory_max_gb = 8.0
        self._memory_java_32bit = False
        self._updating_memory_slider = False
        memory_row = QWidget()
        memory_row_layout = QHBoxLayout(memory_row)
        memory_row_layout.setContentsMargins(0, 0, 0, 0)
        memory_row_layout.setSpacing(12)
        memory_row_layout.addWidget(self.memory_slider, 1)
        memory_row_layout.addWidget(self.memory_value_label, 0)
        memory_row.setFixedHeight(FIELD_HEIGHT)
        self.memory_box = QWidget()
        memory_box_layout = QVBoxLayout(self.memory_box)
        memory_box_layout.setContentsMargins(0, 0, 0, 0)
        memory_box_layout.setSpacing(4)
        memory_box_layout.addWidget(memory_row)
        memory_box_layout.addWidget(self.memory_recommend_label)
        memory_box_layout.addWidget(self.memory_warning_label)
        self.memory_auto_check = QCheckBox(tr("settings.memory_auto"))
        self.memory_auto_check.toggled.connect(self._on_memory_auto_toggled)
        self.concurrency_spin = NoWheelSpinBox()
        self.concurrency_spin.setRange(1, 32)
        self.download_segments_spin = NoWheelSpinBox()
        self.download_segments_spin.setRange(1, 16)
        self.download_segments_spin.setToolTip(tr("settings.download_segments.hint"))
        # WCAG 4.1.2: the spin sits in a box (field + hint), so QFormLayout no longer hands it the
        # row label; name it explicitly like the game-directory / accent fields.
        self.download_segments_spin.setAccessibleName(tr("settings.download_segments"))
        self.download_segments_spin.setAccessibleDescription(
            tr("settings.download_segments.single_hint")
        )
        # A numeric field shows a number, never a word: 1 stays "1", and the sentence that explains
        # what 1 means is a 12px hint under the row instead of replacing the value.
        self.download_segments_hint = QLabel(tr("settings.download_segments.single_hint"))
        self.download_segments_hint.setObjectName("hint")
        self.download_segments_hint.setWordWrap(True)
        self.download_segments_box = QWidget()
        segments_layout = QVBoxLayout(self.download_segments_box)
        segments_layout.setContentsMargins(0, 0, 0, 0)
        segments_layout.setSpacing(4)
        segments_layout.addWidget(self.download_segments_spin)
        segments_layout.addWidget(self.download_segments_hint)
        # Version isolation is a fixed choice, so it is a dropdown like the other enumerated
        # settings. It stays where the checkbox was (same tab position); the old sentence becomes
        # the hint under the row instead of a checkbox label.
        self.isolation_combo = QComboBox()
        self.isolation_combo.addItem(tr("settings.isolation.combo.on"), True)
        self.isolation_combo.addItem(tr("settings.isolation.combo.off"), False)
        self.isolation_combo.setFixedHeight(FIELD_HEIGHT)
        self.isolation_combo.setMinimumWidth(0)
        self.isolation_combo.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.isolation_combo.setAccessibleName(tr("settings.isolation.label"))
        self.isolation_combo.setAccessibleDescription(tr("settings.isolation"))
        self.isolation_hint = QLabel(tr("settings.isolation"))
        self.isolation_hint.setObjectName("hint")
        self.isolation_hint.setWordWrap(True)
        self.isolation_label = QLabel(tr("settings.isolation.label"))
        self.isolation_label.setObjectName("settingsFieldLabel")
        self.isolation_label.setFixedWidth(FIELD_LABEL_WIDTH)
        self.isolation_label.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        self.isolation_label.setWordWrap(True)
        isolation_right = QWidget()
        isolation_right_layout = QVBoxLayout(isolation_right)
        isolation_right_layout.setContentsMargins(0, 0, 0, 0)
        isolation_right_layout.setSpacing(4)
        isolation_right_layout.addWidget(self.isolation_combo)
        isolation_right_layout.addWidget(self.isolation_hint)
        self.isolation_row = QWidget()
        isolation_layout = QHBoxLayout(self.isolation_row)
        isolation_layout.setContentsMargins(0, 0, 0, 0)
        isolation_layout.setSpacing(16)
        isolation_layout.addWidget(self.isolation_label)
        isolation_layout.addWidget(isolation_right, 1)
        self.demo_check = QCheckBox(tr("launch.demo"))
        self.after_launch_combo = QComboBox()
        for code, label in (
            ("keep", tr("settings.after_launch.keep")),
            ("hide", tr("settings.after_launch.hide")),
            ("exit", tr("settings.after_launch.exit")),
        ):
            self.after_launch_combo.addItem(label, code)
        self.trim_memory_check = QCheckBox(tr("settings.trim_memory"))
        self.analysis_check = QCheckBox(tr("settings.auto_crash_analysis"))
        # Accessibility prompt policy: three visible answers instead of a dropdown, each a stop a
        # screen reader can read on its own. The hint is part of the group, not a tooltip: it is the
        # one sentence that keeps "never" from reading as "turn the game's accessibility off".
        self.accessibility_group = AccessibilityPreferenceGroup()
        self.accessibility_hint = QLabel(tr("settings.accessibility.hint"))
        self.accessibility_hint.setObjectName("hint")
        self.accessibility_hint.setWordWrap(True)
        self.accessibility_box = QGroupBox(tr("settings.accessibility"))
        accessibility_layout = QVBoxLayout(self.accessibility_box)
        accessibility_layout.setContentsMargins(12, 12, 12, 12)
        accessibility_layout.setSpacing(8)
        accessibility_layout.addWidget(self.accessibility_hint)
        accessibility_layout.addWidget(self.accessibility_group)
        self.encrypt_check = QCheckBox()
        # This row also holds the password button: wrap the label at 500px so text + button
        # together stay below the card width in German, where the sentence is the longest.
        self.encrypt_check.setText(
            _wrap_checkbox_text(self.encrypt_check, tr("settings.encrypt_tokens"), 500)
        )
        self.encrypt_button = QPushButton(tr("settings.encrypt_set"))
        self.encrypt_button.setObjectName("secondaryButton")
        self.language_combo = QComboBox()
        for code, label in config.GAME_LANGUAGES:
            label = label if code else tr("settings.game_language.follow")
            self.language_combo.addItem(label, code)
        self.ui_language_combo = QComboBox()
        for code, label in i18n.UI_LANGUAGES:
            self.ui_language_combo.addItem(label, code)
        self.proxy_edit = QLineEdit()
        self.proxy_edit.setPlaceholderText(tr("settings.proxy.placeholder"))
        self.cf_key_edit = QLineEdit()
        self.cf_key_edit.setPlaceholderText(tr("settings.cf_key.hint"))
        self.cf_key_edit.setClearButtonEnabled(True)
        self.speed_limit_spin = NoWheelSpinBox()
        self.speed_limit_spin.setRange(0, 1_048_576)
        self.speed_limit_spin.setSingleStep(100)
        # The number is the value; the unit lives in the row label ("KB/s, 0=unlimited"), the same
        # way the memory row's label carries "GB".
        # No special-value wording: 0 renders as "0" (the label already says 0 = unlimited).
        self.window_mode_combo = QComboBox()
        for code, label in (
            ("default", tr("settings.window_mode.default")),
            ("maximized", tr("settings.window_mode.maximized")),
            ("minimized", tr("settings.window_mode.minimized")),
            ("remember", tr("settings.window_mode.remember")),
        ):
            self.window_mode_combo.addItem(label, code)
        self.theme_combo = QComboBox()
        for code, label in (
            ("system", tr("settings.theme.system")),
            ("dark", tr("settings.theme.dark")),
            ("light", tr("settings.theme.light")),
        ):
            self.theme_combo.addItem(label, code)
        self.radius_combo = QComboBox()
        for code, label in (
            ("compact", tr("settings.radius.compact")),
            ("default", tr("settings.radius.default")),
            ("round", tr("settings.radius.round")),
        ):
            self.radius_combo.addItem(label, code)
        self.open_data_dir_button = QPushButton(tr("settings.open_data_dir"))
        self.open_data_dir_button.setObjectName("secondaryButton")

        # Accent color: free hex input with a live swatch and a reset-to-default button
        self.accent_edit = QLineEdit()
        self.accent_edit.setPlaceholderText(DEFAULT_ACCENTS["dark"])
        self.accent_edit.setMaxLength(7)
        self.accent_edit.setToolTip(tr("settings.accent.hint"))
        self.accent_swatch = QLabel()
        self.accent_swatch.setFixedSize(22, 22)
        self.accent_swatch.setObjectName("accentSwatch")
        self.accent_swatch.setToolTip(tr("settings.accent.hint"))
        self.accent_default_button = QPushButton(tr("settings.accent.default"))
        self.accent_default_button.setObjectName("secondaryButton")
        self.accent_default_button.clicked.connect(lambda: self.accent_edit.setText(""))
        self.accent_pick_button = QPushButton(tr("settings.accent.pick"))
        self.accent_pick_button.setObjectName("secondaryButton")
        self.accent_pick_button.clicked.connect(self._pick_accent_color)
        self.accent_preset_buttons = [
            self._make_preset_button(color) for color in ACCENT_PRESETS
        ]
        self.accent_edit.textChanged.connect(self._update_accent_swatch)

        # UI font: common families first, then every installed family; typing filters the list
        self.font_combo = QComboBox()
        self.font_combo.setEditable(True)
        self.font_combo.addItem(tr("settings.font.default"), "")
        families = QFontDatabase.families()
        common = [name for name in COMMON_FONTS if name in families]
        for family in common:
            self.font_combo.addItem(family, family)
        header_row = self.font_combo.count()  # remember where the section header lands
        self.font_combo.addItem(tr("settings.font.common"), "")
        for family in families:
            if family not in common:
                self.font_combo.addItem(family, family)
        header_item = self.font_combo.model().item(header_row)
        if header_item is not None:
            header_item.setEnabled(False)  # a header row, not a choice
        completer = QCompleter(families, self.font_combo)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        self.font_combo.setCompleter(completer)
        self.font_combo.setToolTip(tr("settings.font"))

        form = QFormLayout()
        style_form(form)
        # PCL's settings row: a 32px field plus this 6px gap is the 38px row pitch. The label
        # column is fixed in _apply_form_metrics() so every field starts on the same vertical line.
        form.setVerticalSpacing(ROW_SPACING)
        # WCAG 4.1.2: a control added straight to a row *is* the row's field, so QFormLayout hands it the
        # label text as its accessible name. Wrapping a control in a layout instead (the three rows
        # below) breaks that association - the form sees a layout, not a field - which left the game
        # directory and accent fields announced as unnamed edit boxes. The names are set explicitly
        # rather than through a buddy, so they reach UIA on every platform and survive a row being
        # restructured later.
        self.game_dir_edit.setAccessibleName(tr("settings.game_dir"))  # WCAG 4.1.2
        self.accent_edit.setAccessibleName(tr("settings.accent"))  # WCAG 4.1.2
        game_row = QHBoxLayout()
        game_row.addWidget(self.game_dir_edit, 1)
        game_row.addWidget(self.browse_game)
        java_row = QHBoxLayout()
        java_row.addWidget(self.java_path_edit, 1)
        java_row.addWidget(self.browse_java)
        form.addRow(tr("settings.game_dir"), game_row)
        form.addRow(tr("settings.java"), java_row)
        form.addRow(tr("settings.memory") + tr("unit.gb.label"), self.memory_box)
        form.addRow("", self.memory_auto_check)
        form.addRow(tr("settings.concurrency"), self.concurrency_spin)
        form.addRow(tr("settings.download_segments"), self.download_segments_box)
        form.addRow(tr("settings.speed_limit"), self.speed_limit_spin)
        form.addRow(tr("settings.proxy"), self.proxy_edit)
        form.addRow(tr("settings.cf_key"), self.cf_key_edit)
        form.addRow(tr("settings.window_mode"), self.window_mode_combo)
        form.addRow(tr("settings.theme"), self.theme_combo)
        accent_row = QHBoxLayout()
        accent_row.addWidget(self.accent_edit, 1)
        accent_row.addWidget(self.accent_swatch)
        accent_row.addWidget(self.accent_pick_button)
        accent_row.addWidget(self.accent_default_button)
        form.addRow(tr("settings.accent"), accent_row)
        preset_row = QHBoxLayout()
        preset_row.setSpacing(6)
        for button in self.accent_preset_buttons:
            preset_row.addWidget(button)
        preset_row.addStretch(1)
        form.addRow("", preset_row)
        form.addRow(tr("settings.radius"), self.radius_combo)
        form.addRow(tr("settings.font"), self.font_combo)
        form.addRow(tr("settings.game_language"), self.language_combo)
        form.addRow(tr("settings.ui_language"), self.ui_language_combo)
        form.addRow(tr("settings.after_launch"), self.after_launch_combo)
        self._apply_form_metrics(form)

        encrypt_row = QHBoxLayout()
        encrypt_row.addWidget(self.encrypt_check, 1)
        encrypt_row.addWidget(self.encrypt_button)

        buttons_row = QHBoxLayout()
        buttons_row.addWidget(self.open_data_dir_button)
        buttons_row.addStretch(1)

        inner = QWidget()
        inner.setObjectName("settingsCard")
        layout = QVBoxLayout(inner)
        # The card's 16px inner padding and 12px block gap are layout values, not sheet values.
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)
        layout.addLayout(form)
        layout.addWidget(self.accessibility_box)
        layout.addWidget(self.isolation_row)
        layout.addWidget(self.demo_check)
        layout.addWidget(self.trim_memory_check)
        layout.addWidget(self.analysis_check)
        layout.addLayout(encrypt_row)
        layout.addLayout(buttons_row)
        layout.addStretch(1)

        # Wrap the form in a scroll area so the main window can stay compact
        scroll = QScrollArea()
        scroll.setAccessibleName(tr("nav.settings"))  # WCAG 4.1.2
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(inner)
        outer = QVBoxLayout(self)
        style_page_layout(outer)
        add_page_top_inset(outer)
        outer.addWidget(scroll)

        self.browse_game.clicked.connect(self._browse_game)
        self.browse_java.clicked.connect(self._browse_java)
        self.encrypt_button.clicked.connect(self._change_password)
        self.open_data_dir_button.clicked.connect(self._open_data_dir)
        self.theme_combo.currentIndexChanged.connect(self._update_accent_swatch)
        self._reserve_memory_value_width()
        self.load()
        self._connect_autosave()

    def _reserve_memory_value_width(self) -> None:
        """Give the memory read-out the width of its widest value, so the row never re-flows.

        The read-out is text, so its width followed the number: dragging the slider from "4.0" to
        "12.0" widened the row, which raised the page's minimum width and pushed the window wider.
        Reserving the widest value the field can ever show (``CONFIG_MAX_HEAP_GB``, one decimal) and
        right-aligning it means the digits change while the geometry does not. ``minimumWidth``, not
        ``setFixedWidth``: a larger UI font or a higher DPI may still need more room, and a fixed
        width would clip it.

        Called after the page layout is assembled (so the scoped QSS font is the one being measured)
        and before ``load()`` (so the first value already lands in a stable row).
        """
        from launcher.memory import CONFIG_MAX_HEAP_GB

        label = self.memory_value_label
        label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        label.setText(f"{CONFIG_MAX_HEAP_GB:.1f}")
        label.ensurePolished()
        label.setMinimumWidth(label.sizeHint().width())
        label.clear()

    def _connect_autosave(self) -> None:
        """There is no Save button: every control writes its value as soon as it changes.

        Connected after ``load()`` so filling the widgets from the config does not
        immediately write everything back.
        """

        def on_change(signal) -> None:
            signal.connect(lambda *_: self.save())

        for combo in (
            self.theme_combo,
            self.radius_combo,
            self.ui_language_combo,
            self.language_combo,
            self.after_launch_combo,
            self.window_mode_combo,
            self.isolation_combo,
            self.font_combo,
        ):
            on_change(combo.currentIndexChanged)
        for check in (
            self.memory_auto_check,
            self.demo_check,
            self.trim_memory_check,
            self.analysis_check,
            self.encrypt_check,
        ):
            on_change(check.toggled)
        on_change(self.accessibility_group.changed)
        for spin in (
            self.concurrency_spin,
            self.download_segments_spin,
            self.speed_limit_spin,
        ):
            on_change(spin.valueChanged)
        for edit in (
            self.game_dir_edit,
            self.java_path_edit,
            self.proxy_edit,
            self.cf_key_edit,
            self.accent_edit,
        ):
            on_change(edit.editingFinished)
        font_line = self.font_combo.lineEdit()
        if font_line is not None:
            on_change(font_line.editingFinished)
        self.accent_default_button.clicked.connect(lambda *_: self.save())
        # Drag updates the read-out on every step but writes once, on release; a keyboard step
        # (not sliderDown) still saves immediately, like every other control on the page.
        self.memory_slider.valueChanged.connect(self._on_memory_slider_changed)
        self.memory_slider.sliderReleased.connect(self.save)
        # A different Java can change the upper bound (a 32-bit JVM caps it at 1.5 GB).
        self.java_path_edit.editingFinished.connect(self._refresh_memory_bounds)

    def _apply_form_metrics(self, form: QFormLayout) -> None:
        """PCL's settings metrics: fixed left-aligned label column, 32px fields, 38px row pitch.

        Done after the rows exist because QFormLayout builds the label widget itself when a
        plain string is passed. Rows whose field is a layout (game directory, Java path,
        accent) are measured through the layout's child widgets; the two browse buttons and
        the three spin boxes get the same height as a text field.
        """
        fields = (
            self.game_dir_edit,
            self.java_path_edit,
            self.proxy_edit,
            self.cf_key_edit,
            self.accent_edit,
            self.concurrency_spin,
            self.download_segments_spin,
            self.speed_limit_spin,
            self.window_mode_combo,
            self.theme_combo,
            self.radius_combo,
            self.font_combo,
            self.language_combo,
            self.ui_language_combo,
            self.after_launch_combo,
        )
        for widget in fields:
            widget.setFixedHeight(FIELD_HEIGHT)
            # The path text otherwise becomes the card's minimum width, which is wider than
            # the window at the 930px floor; the field may shrink and scroll its text instead.
            widget.setMinimumWidth(0)
            widget.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        for button in (self.browse_game, self.browse_java):
            button.setFixedHeight(FIELD_HEIGHT)
            button.setMinimumWidth(0)
        for row in range(form.rowCount()):
            label_item = form.itemAt(row, QFormLayout.ItemRole.LabelRole)
            if label_item is not None and label_item.widget() is not None:
                label = label_item.widget()
                label.setObjectName("settingsFieldLabel")
                label.setFixedWidth(FIELD_LABEL_WIDTH)
                label.setAlignment(
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
                )
                label.setWordWrap(True)
            field_item = form.itemAt(row, QFormLayout.ItemRole.FieldRole)
            if field_item is not None and field_item.layout() is not None:
                layout = field_item.layout()
                for index in range(layout.count()):
                    child = layout.itemAt(index).widget()
                    is_field = isinstance(child, (QLineEdit, QComboBox))
                    is_browse = child is self.browse_game or child is self.browse_java
                    if is_field or is_browse:
                        child.setFixedHeight(FIELD_HEIGHT)

    def load(self) -> None:
        cfg, _ = config.load()
        self.game_dir_edit.setText(str(cfg.game_dir) if cfg.game_dir else "")
        # Long paths otherwise scroll to their tail the moment the page opens; showing the drive
        # letter is what tells the user which folder this is.
        self.game_dir_edit.setCursorPosition(0)
        self.java_path_edit.setText(str(cfg.java_path) if cfg.java_path else "")
        self.java_path_edit.setCursorPosition(0)
        self._memory_value = float(cfg.memory_gb)
        self._refresh_memory_bounds()
        self.memory_auto_check.setChecked(cfg.memory_auto)
        self.concurrency_spin.setValue(cfg.max_concurrent_downloads)
        self.download_segments_spin.setValue(cfg.download_segments)
        isolation_index = self.isolation_combo.findData(bool(cfg.default_isolation))
        self.isolation_combo.setCurrentIndex(max(isolation_index, 0))
        self.demo_check.setChecked(cfg.demo_mode)
        idx = self.after_launch_combo.findData(cfg.after_launch_behavior)
        self.after_launch_combo.setCurrentIndex(max(idx, 0))
        self.trim_memory_check.setChecked(cfg.trim_memory_on_launch)
        self.analysis_check.setChecked(cfg.auto_crash_analysis)
        # "auto" is what an unknown/hand-edited value means; the config validator already normalises it.
        self.accessibility_group.set_value(cfg.accessibility_preference)
        self.encrypt_check.setChecked(cfg.token_encryption)
        self._update_encrypt_button()
        index = self.language_combo.findData(cfg.game_language)
        self.language_combo.setCurrentIndex(max(index, 0))
        index2 = self.ui_language_combo.findData(cfg.ui_language)
        self.ui_language_combo.setCurrentIndex(max(index2, 0))
        self.speed_limit_spin.setValue(cfg.download_speed_limit_kb)
        index3 = self.window_mode_combo.findData(cfg.window_start_mode)
        self.window_mode_combo.setCurrentIndex(max(index3, 0))
        index4 = self.theme_combo.findData(cfg.theme)
        self.theme_combo.setCurrentIndex(max(index4, 0))
        index5 = self.radius_combo.findData(cfg.ui_radius)
        self.radius_combo.setCurrentIndex(max(index5, 0))
        self.accent_edit.setText(cfg.accent_color or "")
        self._update_accent_swatch()
        font_index = self.font_combo.findData(cfg.ui_font or "")
        if font_index >= 0:
            self.font_combo.setCurrentIndex(font_index)
        elif cfg.ui_font:
            self.font_combo.setEditText(cfg.ui_font)
        self.proxy_edit.setText(cfg.http_proxy or "")
        self.cf_key_edit.setText(cfg.curseforge_api_key or "")

    def _make_preset_button(self, color: str) -> QPushButton:
        """One-click accent swatch."""
        button = AccentPresetButton(color, accent_focus_ring())
        button.clicked.connect(lambda *_, value=color: self._set_accent(value))
        return button
    def _set_accent(self, color: str) -> None:
        self.accent_edit.setText(color)
        self.save()

    def _pick_accent_color(self) -> None:
        """System colour picker for the accent."""
        from PySide6.QtGui import QColor

        current = QColor(normalize_hex(self.accent_edit.text()) or DEFAULT_ACCENTS["dark"])
        chosen = QColorDialog.getColor(current, self, tr("settings.accent"))
        if chosen.isValid():
            self._set_accent(chosen.name())

    def _update_accent_swatch(self) -> None:
        """Paint the swatch with the typed color, falling back to the theme default."""
        theme = resolve_theme(self.theme_combo.currentData() or "dark")
        fallback = DEFAULT_ACCENTS.get(theme, DEFAULT_ACCENTS["dark"])
        color = normalize_hex(self.accent_edit.text()) or fallback
        # only the fill is set here: the outline comes from the theme's QSS
        self.accent_swatch.setStyleSheet(f"background: {color};")

    def _selected_font(self) -> str:
        """Chosen font family: the picked list entry, or whatever the user typed."""
        index = self.font_combo.currentIndex()
        if index >= 0 and self.font_combo.currentText() == self.font_combo.itemText(index):
            return str(self.font_combo.itemData(index) or "")
        return self.font_combo.currentText().strip()

    def save(self) -> None:
        cfg, cfg_path = config.load()
        want_encryption = self.encrypt_check.isChecked()
        if want_encryption and not cfg.token_encryption:
            if not self._enable_encryption(cfg, cfg_path):
                self.encrypt_check.setChecked(False)  # revert the checked state
                return
            cfg, cfg_path = config.load()
        elif not want_encryption and cfg.token_encryption:
            if not self._disable_encryption(cfg, cfg_path):
                self.encrypt_check.setChecked(True)  # keep it enabled
                return
            cfg, cfg_path = config.load()
        cfg.game_dir = self.game_dir_edit.text().strip() or None
        cfg.java_path = self.java_path_edit.text().strip() or None
        cfg.memory_gb = float(self._memory_value)
        cfg.memory_auto = self.memory_auto_check.isChecked()
        cfg.max_concurrent_downloads = self.concurrency_spin.value()
        cfg.download_segments = self.download_segments_spin.value()
        cfg.default_isolation = bool(self.isolation_combo.currentData())
        cfg.demo_mode = self.demo_check.isChecked()
        cfg.after_launch_behavior = self.after_launch_combo.currentData() or "keep"
        cfg.trim_memory_on_launch = self.trim_memory_check.isChecked()
        cfg.auto_crash_analysis = self.analysis_check.isChecked()
        cfg.accessibility_preference = self.accessibility_group.value()
        cfg.game_language = self.language_combo.currentData()
        cfg.ui_language = self.ui_language_combo.currentData()
        cfg.download_speed_limit_kb = self.speed_limit_spin.value()
        cfg.http_proxy = self.proxy_edit.text().strip()
        cfg.curseforge_api_key = self.cf_key_edit.text().strip()
        cfg.window_start_mode = self.window_mode_combo.currentData()
        cfg.theme = self.theme_combo.currentData()
        cfg.ui_radius = self.radius_combo.currentData()
        accent_input = self.accent_edit.text().strip()
        accent = normalize_hex(accent_input)
        if accent_input and accent is None:
            self.accent_edit.setText("")  # invalid input: fall back to the theme default
        cfg.accent_color = accent or ""
        cfg.ui_font = self._selected_font()
        config.save(cfg, cfg_path)
        self._update_accent_swatch()
        # Theme, accent color and UI font take effect immediately
        from gui.theme import apply_theme

        apply_theme(cfg.theme, cfg.accent_color, cfg.ui_font, cfg.ui_radius)
        # Proxy / CurseForge key changes need a fresh HTTP connection pool
        from launcher.meta.manifest import reset_http_client

        reset_http_client()
        self._update_encrypt_button()
        set_app_status(
            self,
            tr("settings.accent.invalid")
            if (accent_input and accent is None)
            else tr("settings.autosave"),
        )
        self.settings_changed.emit()

    def _on_memory_auto_toggled(self, checked: bool) -> None:
        """Auto mode owns the value; the slider and its read-out go quiet, not away."""
        self.memory_slider.setEnabled(not checked)
        self.memory_value_label.setEnabled(not checked)

    def _refresh_memory_bounds(self) -> None:
        """Recompute the device range (and the 32-bit ceiling) and clamp the thumb, not the value.

        ``_memory_value`` is deliberately left alone when it sits outside ``[min, max]``: the
        config may hold a larger heap from another machine, and opening this page must not
        rewrite it. The thumb parks at the nearest end and a grey line says what was stored.
        """
        from launcher.java.locate import selected_java_bits
        from launcher.memory import HEAP_STEP_GB, MIN_HEAP_GB, maximum_heap_gb, system_memory_gb

        total_gb, _available = system_memory_gb()
        self._memory_java_32bit = (
            selected_java_bits(self.java_path_edit.text().strip() or None) == 32
        )
        self._memory_min_gb = MIN_HEAP_GB
        self._memory_max_gb = maximum_heap_gb(
            total_gb,
            java_32bit=self._memory_java_32bit,
            minimum_gb=self._memory_min_gb,
        )
        ticks = max(1, round((self._memory_max_gb - self._memory_min_gb) / HEAP_STEP_GB))
        self._updating_memory_slider = True
        try:
            self.memory_slider.setRange(0, ticks)
            self._position_memory_slider()
        finally:
            self._updating_memory_slider = False
        self._update_memory_labels()

    def _position_memory_slider(self) -> None:
        """Park the thumb at the closest legal step to the stored value (guarded against save)."""
        from launcher.memory import HEAP_STEP_GB

        tick = round((self._memory_value - self._memory_min_gb) / HEAP_STEP_GB)
        tick = max(0, min(int(tick), self.memory_slider.maximum()))
        self._updating_memory_slider = True
        try:
            self.memory_slider.setValue(tick)
        finally:
            self._updating_memory_slider = False

    def _on_memory_slider_changed(self, tick: int) -> None:
        """Live read-out while dragging; save once on release or immediately for a key step."""
        if self._updating_memory_slider:
            return
        from launcher.memory import HEAP_STEP_GB

        self._memory_value = round(
            self._memory_min_gb + tick * HEAP_STEP_GB, 2
        )
        self._update_memory_labels()
        if not self.memory_slider.isSliderDown():
            self.save()

    def _update_memory_labels(self) -> None:
        """Paint the GB read-out, the device recommendation and any non-blocking warning."""
        # The field shows the number only; "GB" is in the row label. The accessible description
        # still says the unit out loud, because a screen reader has no label column to lean on.
        value_text = f"{self._memory_value:.1f}"
        self.memory_value_label.setText(value_text)
        self.memory_recommend_label.setText(
            tr("settings.memory_recommend", f"{self._memory_max_gb:g}")
        )
        warnings: list[str] = []
        if self._memory_java_32bit:
            from launcher.memory import JAVA_32BIT_HEAP_LIMIT_GB

            cap = min(self._memory_max_gb, JAVA_32BIT_HEAP_LIMIT_GB)
            warnings.append(tr("settings.memory_java32", f"{cap:g}"))
        if self._memory_value > self._memory_max_gb + 1e-9:
            warnings.append(
                tr(
                    "settings.memory_over_limit",
                    f"{self._memory_value:g}",
                    f"{self._memory_max_gb:g}",
                )
            )
        self.memory_warning_label.setText("\n".join(warnings))
        self.memory_warning_label.setVisible(bool(warnings))
        description = value_text + " " + tr("unit.gb") + " · " + self.memory_recommend_label.text()
        if warnings:
            description += " · " + " ".join(warnings)
        self.memory_slider.setAccessibleDescription(description)

    def _update_encrypt_button(self) -> None:
        cfg, _ = config.load()
        if cfg.token_encryption:
            self.encrypt_button.setText(tr("settings.encrypt_change"))
        else:
            self.encrypt_button.setText(tr("settings.encrypt_set"))

    def _prompt_new_password(self) -> str | None:
        from PySide6.QtWidgets import QInputDialog, QLineEdit

        first, ok = QInputDialog.getText(
            self,
            tr("settings.encrypt_set"),
            tr("settings.encrypt.password.prompt"),
            QLineEdit.EchoMode.Password,
        )
        if not ok or not first:
            return None
        second, ok2 = QInputDialog.getText(
            self,
            tr("settings.encrypt_set"),
            tr("settings.encrypt.password.confirm"),
            QLineEdit.EchoMode.Password,
        )
        if not ok2 or first != second:
            set_app_status(self, tr("settings.encrypt.password.mismatch"))
            return None
        return first

    def _prompt_current_password(self) -> str | None:
        from PySide6.QtWidgets import QInputDialog, QLineEdit

        text, ok = QInputDialog.getText(
            self,
            tr("settings.encrypt_change"),
            tr("settings.encrypt.current.prompt"),
            QLineEdit.EchoMode.Password,
        )
        if not ok or not text:
            return None
        return text

    def _enable_encryption(self, cfg, cfg_path) -> bool:
        from launcher.auth import AccountStore, secure

        password = self._prompt_new_password()
        if password is None:
            return False
        try:
            secure.create_vault(password)
            cfg.token_encryption = True
            config.save(cfg, cfg_path)
            store = AccountStore()
            store.save(store.load())  # re-save as ciphertext with the new password
        except Exception as exc:  # noqa: BLE001 - funnel uniformly to UI status
            set_app_status(self, tr("settings.encrypt.msg.failed", str(exc)), "error")
            return False
        set_app_status(self, tr("settings.encrypt.msg.enabled"))
        return True

    def _disable_encryption(self, cfg, cfg_path) -> bool:
        from launcher.auth import AccountStore, secure

        password = self._prompt_current_password()
        if password is None:
            return False
        if not secure.verify_password(password):
            set_app_status(self, tr("settings.encrypt.wrong"), "error")
            return False
        secure.set_password(password)
        store = AccountStore()
        try:
            accounts = store.load()
        except Exception as exc:  # noqa: BLE001
            secure.forget_password()
            set_app_status(self, tr("settings.encrypt.msg.failed", str(exc)), "error")
            return False
        cfg.token_encryption = False
        config.save(cfg, cfg_path)
        store.save(accounts)  # plaintext
        (paths.launcher_dir() / secure.VAULT_FILENAME).unlink(missing_ok=True)
        secure.forget_password()
        set_app_status(self, tr("settings.encrypt.msg.disabled"))
        return True

    def _change_password(self) -> None:
        cfg, cfg_path = config.load()
        if not cfg.token_encryption:
            self._enable_encryption(cfg, cfg_path)
            if cfg.token_encryption:
                self.encrypt_check.setChecked(True)
                self._update_encrypt_button()
            return
        from launcher.auth import AccountStore, secure

        current = self._prompt_current_password()
        if current is None:
            return
        if not secure.verify_password(current):
            set_app_status(self, tr("settings.encrypt.wrong"), "error")
            return
        new_password = self._prompt_new_password()
        if new_password is None:
            return
        secure.set_password(current)
        store = AccountStore()
        try:
            accounts = store.load()
            secure.create_vault(new_password)
            store.save(accounts)
        except Exception as exc:  # noqa: BLE001
            set_app_status(self, tr("settings.encrypt.msg.failed", str(exc)), "error")
            return
        set_app_status(self, tr("settings.encrypt.msg.changed"))

    def _open_data_dir(self) -> None:
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        target = paths.launcher_dir()
        target.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))
        set_app_status(self, tr("settings.msg.data_dir_opened", str(target)))

    def _browse_game(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, tr("settings.game_dir"), str(paths.default_game_dir()))
        if chosen:
            self.game_dir_edit.setText(chosen)
            self.save()  # picking a folder is a change: no Save button to press

    def _browse_java(self) -> None:
        chosen, _ = QFileDialog.getOpenFileName(self, "Java")
        if chosen:
            self.java_path_edit.setText(chosen)
            self.save()
            self._refresh_memory_bounds()
