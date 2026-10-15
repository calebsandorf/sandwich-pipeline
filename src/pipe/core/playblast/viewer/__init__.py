"""In-DCC playblast viewer."""

from __future__ import annotations

from Qt.QtWidgets import QWidget

from pipe.core.playblast.clip import PreviewClip
from pipe.core.playblast.viewer.window import ClipAction, ViewerWindow


def open_viewer(
    clips: list[PreviewClip],
    parent: QWidget | None,
    *,
    cut: PreviewClip | None = None,
    cut_unavailable: str = "",
    action: ClipAction | None = None,
) -> None:
    """Open a viewer window on `clips`"""
    ViewerWindow(
        clips, parent, cut=cut, cut_unavailable=cut_unavailable, action=action
    ).show()


__all__ = ["ClipAction", "open_viewer"]
