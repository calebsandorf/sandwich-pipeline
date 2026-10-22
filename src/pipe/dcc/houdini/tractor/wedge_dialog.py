"""The Wedge Send dialog: the parms to wedge and their values, the frames, what
the flipbook draws, and how the job is named and run."""

from __future__ import annotations

import math
import shlex
from collections.abc import Callable
from dataclasses import dataclass

import hou
from Qt import QtCore, QtGui, QtWidgets

from pipe.core.ui.dialogs import DialogButtons
from pipe.dcc.houdini.tractor import SendRefused
from pipe.dcc.houdini.tractor.cache_dialog import BLADES, PRIORITY
from pipe.dcc.houdini.tractor import wedges
from pipe.dcc.houdini.tractor.wedges import Wedge

# A parm path and the values typed for it.
Row = tuple[str, str]
# Turns the rows into wedges, or raises SendRefused saying what is wrong.
Check = Callable[[list[Row]], list[Wedge]]

# One wedge is just a farm sim.
MINIMUM = 2
# The Draw menu, the default first.
DRAWS = {"Shot": wedges.SHOT, "Cache only": wedges.CACHE}
DRAW_HELP = (
    "Shot draws the stage at the publish node, through the shot camera.\n"
    "Cache only draws just this cache, through the same camera."
)
AFTER_HELP = (
    "Caches loading from disk between this one and the publish node.\n"
    "A checked cache is written again from each wedge, into the wedge's folder.\n"
    "An unchecked one keeps its files."
)
_WIDTH = 520
# Houdini's own icon for removing a multiparm instance.
_REMOVE_ICON = "BUTTONS_multi_remove"


@dataclass(frozen=True)
class WedgeChoice:
    title: str
    priority: int
    service: str
    start: int
    end: int
    draw: str
    # The paths of the caches checked under Cache after.
    after: list[str]
    wedges: list[Wedge]
    # As typed, so the next dialog for this node starts from them.
    rows: list[Row]


# The last dialog of each node, raised when its button is pressed again.
_dialogs: dict[str, _WedgeDialog] = {}


def show(
    parent: QtWidgets.QWidget | None,
    node: hou.Node,
    title: str,
    frames: tuple[int, int],
    after: list[hou.Node],
    rows: list[Row],
    check: Check,
    send: Callable[[WedgeChoice], None],
) -> None:
    """Open the dialog, which calls `send` on Send. `after` lists the caches
    loading from disk between the node and the publish node."""
    path = node.path()
    old = _dialogs.get(path)
    if old is not None and old.isVisible():
        old.raise_()
        old.activateWindow()
        return
    if old is not None:
        old.deleteLater()
    dialog = _WedgeDialog(parent, node, title, frames, after, rows, check, send)
    _dialogs[path] = dialog
    dialog.show()


def count(rows: list[Row]) -> int:
    """How many wedges the rows make, before their values are checked."""
    sizes = []
    for path, values in rows:
        if not path.strip():
            continue
        try:
            sizes.append(len(shlex.split(values)))
        except ValueError:
            # An unclosed quote, which Send explains.
            sizes.append(0)
    return math.prod(sizes) if sizes else 0


class _ParmField(QtWidgets.QLineEdit):
    """Takes a parm dragged from the parameter pane."""

    def __init__(self, text: str) -> None:
        super().__init__(text)
        self.setPlaceholderText("Drag a parm here")

    # Qt's stubs name the event `arg__1`, hence the override ignores. hou.qt
    # is Houdini's own Python, which the hou stubs leave out.
    def dragEnterEvent(
        self, event: QtGui.QDragEnterEvent
    ) -> None:  # ty: ignore[invalid-method-override]
        kind = hou.qt.mimeType.parmPath  # ty: ignore[unresolved-attribute]
        if event.mimeData().hasFormat(kind):
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dropEvent(
        self, event: QtGui.QDropEvent
    ) -> None:  # ty: ignore[invalid-method-override]
        kind = hou.qt.mimeType.parmPath  # ty: ignore[unresolved-attribute]
        data = event.mimeData().data(kind)
        if data.isEmpty():
            super().dropEvent(event)
            return
        # Several dragged parms come separated by tabs; a row holds one.
        self.setText(bytes(data.data()).decode().split("\t")[0])
        event.acceptProposedAction()


def _choose(field: QtWidgets.QLineEdit, chosen: list[hou.Parm | None]) -> None:
    # Houdini's chooser sends a list, empty when it is cancelled.
    if chosen and chosen[0] is not None:
        field.setText(chosen[0].path())


class _WedgeDialog(QtWidgets.QDialog, DialogButtons):
    def __init__(
        self,
        parent: QtWidgets.QWidget | None,
        node: hou.Node,
        title: str,
        frames: tuple[int, int],
        after: list[hou.Node],
        rows: list[Row],
        check: Check,
        send: Callable[[WedgeChoice], None],
    ) -> None:
        super().__init__(parent)
        self._init_buttons(True, "Send", "Cancel")
        self.setWindowTitle(f"Wedge {node.name()} on Tractor")
        self.setMinimumWidth(_WIDTH)
        self._default_title = title
        self._check = check
        self._send = send
        self._wedges: list[Wedge] = []
        self._fields: list[tuple[_ParmField, QtWidgets.QLineEdit]] = []
        # Every widget of each row, in the order of `_fields`.
        self._rows: list[list[QtWidgets.QWidget]] = []

        self._title = QtWidgets.QLineEdit()
        self._title.setPlaceholderText(title)

        low, high, default = PRIORITY
        self._priority = QtWidgets.QSpinBox()
        self._priority.setRange(low, high)
        self._priority.setValue(default)

        self._blades = QtWidgets.QComboBox()
        self._blades.addItems(list(BLADES))

        self._start = QtWidgets.QSpinBox()
        self._end = QtWidgets.QSpinBox()
        frame_row = QtWidgets.QHBoxLayout()
        for box, value in ((self._start, frames[0]), (self._end, frames[1])):
            box.setRange(-100000, 100000)
            box.setValue(value)
            box.valueChanged.connect(self._update_count)
            frame_row.addWidget(box)
        frame_row.addStretch(1)

        self._draw = QtWidgets.QComboBox()
        self._draw.addItems(list(DRAWS))
        self._draw.setToolTip(DRAW_HELP)
        self._draw.currentTextChanged.connect(self._update_after)

        # Checked, since a cache keeping its files hides the wedge behind it.
        self._after = QtWidgets.QListWidget()
        self._after.setToolTip(AFTER_HELP)
        for cache in after:
            item = QtWidgets.QListWidgetItem(cache.path())
            item.setFlags(item.flags() | QtCore.Qt.ItemIsUserCheckable)
            item.setCheckState(QtCore.Qt.Checked)
            self._after.addItem(item)

        self._grid = QtWidgets.QGridLayout()
        self._grid.setContentsMargins(0, 0, 0, 0)
        for path, values in rows or [("", "")]:
            self._add_row(path, values)
        add = QtWidgets.QPushButton("Add Parm")
        add.clicked.connect(lambda: self._add_row("", ""))
        wedge_column = QtWidgets.QVBoxLayout()
        wedge_column.addLayout(self._grid)
        wedge_column.addWidget(add)
        self._count = QtWidgets.QLabel()

        form = QtWidgets.QFormLayout()
        form.addRow("Title", self._title)
        form.addRow("Priority", self._priority)
        form.addRow("Blades", self._blades)
        form.addRow("Frames", frame_row)
        form.addRow("Draw", self._draw)
        if after:
            form.addRow("Cache after", self._after)
        form.addRow("Wedge", wedge_column)
        form.addRow("", self._count)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.addLayout(form)
        layout.addWidget(self.buttons)
        self._update_count()

    def _add_row(self, path: str, values: str) -> None:
        parm = _ParmField(path)
        chooser = hou.qt.ParmChooserButton()  # ty: ignore[unresolved-attribute]
        chooser.parmSelected.connect(lambda chosen: _choose(parm, chosen))
        value_field = QtWidgets.QLineEdit(values)
        value_field.setPlaceholderText("Values, like 0.5 1 2")
        for field in (parm, value_field):
            field.textChanged.connect(self._update_count)
        remove = QtWidgets.QToolButton()
        remove.setIcon(hou.qt.Icon(_REMOVE_ICON))
        remove.setAutoRaise(True)
        remove.setToolTip("Remove this parm")
        widgets = [parm, chooser, value_field, remove]
        remove.clicked.connect(lambda: self._remove_row(widgets))
        row = self._grid.rowCount()
        for column, widget in enumerate(widgets):
            self._grid.addWidget(widget, row, column)
        self._fields.append((parm, value_field))
        self._rows.append(widgets)

    def _remove_row(self, widgets: list[QtWidgets.QWidget]) -> None:
        index = self._rows.index(widgets)
        if len(self._rows) == 1:
            # The last row is cleared, so there is always one to fill.
            for field in self._fields[0]:
                field.clear()
            return
        del self._rows[index]
        del self._fields[index]
        for widget in widgets:
            self._grid.removeWidget(widget)
            widget.deleteLater()
        self._update_count()

    def rows(self) -> list[Row]:
        return [
            (parm.text().strip(), values.text().strip())
            for parm, values in self._fields
            if parm.text().strip() or values.text().strip()
        ]

    def _update_after(self) -> None:
        # Cache only draws the wedged cache alone, so nothing after it matters.
        self._after.setEnabled(DRAWS[self._draw.currentText()] == wedges.SHOT)

    def _update_count(self) -> None:
        total = count(self.rows())
        self._count.setText(f"{total} wedge" if total == 1 else f"{total} wedges")
        send = self.buttons.button(QtWidgets.QDialogButtonBox.Ok)
        send.setEnabled(total >= MINIMUM and self._start.value() <= self._end.value())

    def accept(self) -> None:
        try:
            self._wedges = self._check(self.rows())
        except SendRefused as refusal:
            # The dialog stays open, so the rows aren't typed again.
            QtWidgets.QMessageBox.warning(self, self.windowTitle(), str(refusal))
            return
        super().accept()
        self._send(self.choice())

    def choice(self) -> WedgeChoice:
        return WedgeChoice(
            title=self._title.text().strip() or self._default_title,
            priority=self._priority.value(),
            service=BLADES[self._blades.currentText()],
            start=self._start.value(),
            end=self._end.value(),
            draw=DRAWS[self._draw.currentText()],
            after=[
                item.text()
                for item in (self._after.item(i) for i in range(self._after.count()))
                if item.checkState() == QtCore.Qt.Checked
            ],
            wedges=self._wedges,
            rows=self.rows(),
        )
