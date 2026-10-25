"""Wedge review: the Review Wedges button of a File Cache. Every finished wedge
of the node's runs plays in the playblast viewer, newest run first. Apply
Values sets the wedge on screen in the hip, and Delete removes it from disk."""

from __future__ import annotations

import shutil
from pathlib import Path

import hou

from pipe.core.playblast import PreviewClip
from pipe.core.playblast.viewer import ClipAction, open_viewer
from pipe.dcc.houdini import runtime
from pipe.dcc.houdini.tractor import snapshot, wedges

TITLE = "Review Wedges"


def review(node: hou.Node) -> None:
    runs, unread = _runs(node)
    if unread:
        hou.ui.displayMessage(
            "Review Wedges leaves out the runs it can't read:\n" + "\n".join(unread),
            title=TITLE,
            severity=hou.severityType.Warning,
        )
    if not runs and not unread:
        hou.ui.displayMessage(
            f"No wedge run in {snapshot.folder(node)} was sent from {node.path()}. A node "
            "renamed or moved since leaves its runs behind. Wedge on Tractor "
            "sends one.",
            title=TITLE,
        )
    if not runs:
        return
    clips: list[PreviewClip] = []
    # The run and wedge of each clip, by its frames folder.
    shown: dict[Path, tuple[wedges.Run, wedges.Wedge]] = {}
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
            shown[frames] = (run, wedge)
    if not clips:
        hou.ui.displayMessage(
            f"No wedge of {node.path()} has finished yet. Its last run is "
            f"{runs[0].folder}. Check its job in Tractor.",
            title=TITLE,
        )
        return
    actions = [
        ClipAction(
            "Apply Values",
            lambda clip: _apply(*shown[clip.frames_dir]),
            "Set this wedge's values in the hip.",
        ),
        ClipAction(
            "Delete",
            lambda clip: _delete(*shown[clip.frames_dir]),
            "Delete this wedge's cache and flipbook from disk.",
        ),
    ]
    open_viewer(clips, runtime.get_main_qt_window(), actions=actions)


def _runs(node: hou.Node) -> tuple[list[wedges.Run], list[str]]:
    """The node's wedge runs, newest first, and why each record that can't be
    read wasn't."""
    records = sorted(snapshot.folder(node).glob(f"v*/{wedges.RECORD}"), reverse=True)
    runs = []
    unread = []
    for record in records:
        try:
            run = wedges.read_record(record)
        except ValueError as error:
            unread.append(f"{record.parent}: {error}")
            continue
        # Two nodes of one name share a Send folder.
        if run.node == node.path():
            runs.append(run)
    return runs, unread


def _apply(run: wedges.Run, wedge: wedges.Wedge) -> bool:
    name = f"{run.folder.name} {wedge.name}"
    parms = [parm for path in wedge.values if (parm := hou.parm(path)) is not None]
    locked = [parm.path() for parm in parms if parm.isLocked()]
    if locked:
        hou.ui.displayMessage(
            f"{', '.join(locked)} is locked, so nothing of {name} was set. "
            "Unlock it, then Apply again.",
            title=TITLE,
            severity=hou.severityType.Warning,
        )
        return False
    lost = [f"{parm.path()}: {_held(parm)}" for parm in parms if parm.keyframes()]
    if lost:
        pressed = hou.ui.displayMessage(
            f"Applying {name} replaces what these parms hold:\n" + "\n".join(lost),
            buttons=("Apply", "Cancel"),
            default_choice=1,
            close_choice=1,
            title=TITLE,
            severity=hou.severityType.Warning,
        )
        if pressed != 0:
            return False
    with hou.undos.group(f"Apply wedge {name}"):
        missing = wedges.set_values(wedge)
    if missing:
        hou.ui.displayMessage(
            f"The hip no longer has {', '.join(missing)}, so those values of "
            f"{name} were not set.",
            title=TITLE,
            severity=hou.severityType.Warning,
        )
        return False
    hou.ui.setStatusMessage(
        f"Applied {name}: {', '.join(wedges.describe(wedge))}. "
        "Save to Disk to cache it."
    )
    return False


def _held(parm: hou.Parm) -> str:
    keys = parm.keyframes()
    if len(keys) > 1:
        return f"{len(keys)} keyframes"
    # A channel reference is an expression too, such as ch("../tx").
    return keys[0].expression() if keys[0].isExpressionSet() else "a keyframe"


def _delete(run: wedges.Run, wedge: wedges.Wedge) -> bool:
    name = f"{run.folder.name} {wedge.name}"
    # Deleted from another viewer of the same runs.
    if not (run.folder / wedge.name).exists():
        hou.ui.setStatusMessage(f"{name} was already deleted")
        return True
    pressed = hou.ui.displayMessage(
        f"Delete {name}? Its cache and flipbook can't be recovered.",
        buttons=("Delete", "Cancel"),
        default_choice=1,
        close_choice=1,
        title=TITLE,
        severity=hou.severityType.Warning,
    )
    if pressed != 0:
        return False
    shutil.rmtree(run.folder / wedge.name)
    # Send makes every wedge's folder, so none left means none is still to
    # run, and the run's snapshot and record go with the last one.
    if not any((run.folder / w.name).exists() for w in run.wedges):
        shutil.rmtree(run.folder)
    hou.ui.setStatusMessage(f"Deleted {name}")
    return True
