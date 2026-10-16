"""In-DCC playblast viewer."""

from __future__ import annotations

from collections.abc import Sequence

from Qt.QtWidgets import QWidget

from pipe.core.playblast.clip import PreviewClip
from pipe.core.playblast.viewer.window import ClipAction, ViewerWindow


def open_viewer(
    clips: list[PreviewClip],
    parent: QWidget | None,
    *,
    cut: PreviewClip | None = None,
    cut_unavailable: str = "",
    actions: Sequence[ClipAction] = (),
) -> None:
    """Open a viewer window on `clips`"""
    ViewerWindow(
        clips, parent, cut=cut, cut_unavailable=cut_unavailable, actions=actions
    ).show()


__all__ = ["ClipAction", "open_viewer"]
