"""Wedge review: the Review Wedges button of a File Cache. Every finished wedge
of the node's runs plays in the playblast viewer, newest run first, and Apply
Values sets the wedge on screen in the hip."""

from __future__ import annotations

from pathlib import Path

import hou

from pipe.core.cache import SIM_DIRNAME
from pipe.core.playblast import PreviewClip
from pipe.core.playblast.viewer import ClipAction, open_viewer
from pipe.dcc.houdini import runtime
from pipe.dcc.houdini.tractor import wedges
from pipe.dcc.houdini.tractor.cache_send import SEND_FOLDER

TITLE = "Review Wedges"


def review(node: hou.Node) -> None:
    runs = _runs(node)
    if not runs:
        hou.ui.displayMessage(
            f"{node.path()} has no wedge runs. Wedge on Tractor sends one.",
            title=TITLE,
        )
        return
    clips: list[PreviewClip] = []
    shown: list[tuple[wedges.Run, wedges.Wedge]] = []
    for run in runs:
        for wedge in run.wedges:
            frames = run.folder / wedge.name / wedges.FLIPBOOK
            # The blade renames the folder into place once every frame is burned.
            if not frames.is_dir():
                continue
            clips.append(
                PreviewClip(
                    label=f"{run.folder.name} {wedge.name}: "
                    + ", ".join(wedges.describe(wedge)),
                    frames_dir=frames,
                    frames_basename=wedges.FRAMES,
                    frame_start=run.start,
                    frame_end=run.end,
                    fps=round(run.fps),
                )
            )
            shown.append((run, wedge))
    if not clips:
        hou.ui.displayMessage(
            f"No wedge of {node.path()} has finished yet. Its last run is "
            f"{runs[0].folder}. Check its job in Tractor.",
            title=TITLE,
        )
        return
    action = ClipAction(
        "Apply Values",
        lambda index: _apply(*shown[index]),
        "Set this wedge's values in the hip.",
    )
    open_viewer(clips, runtime.get_main_qt_window(), action=action)


def _runs(node: hou.Node) -> list[wedges.Run]:
    """The node's wedge runs, newest first."""
    folder = Path(hou.text.expandString("$HIP")) / SIM_DIRNAME / SEND_FOLDER
    records = sorted((folder / node.name()).glob(f"v*/{wedges.RECORD}"), reverse=True)
    # Two nodes of one name share a Send folder.
    runs = [wedges.read_record(record) for record in records]
    return [run for run in runs if run.node == node.path()]


def _apply(run: wedges.Run, wedge: wedges.Wedge) -> None:
    name = f"{run.folder.name} {wedge.name}"
    with hou.undos.group(f"Apply wedge {name}"):
        missing = wedges.set_values(wedge)
    if missing:
        hou.ui.displayMessage(
            f"The hip no longer has {', '.join(missing)}, so those values of "
            f"{name} were not set.",
            title=TITLE,
            severity=hou.severityType.Warning,
        )
        return
    hou.ui.setStatusMessage(f"Applied {name}: {', '.join(wedges.describe(wedge))}")
