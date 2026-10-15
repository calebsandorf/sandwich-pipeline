"""Wedge Send: the Wedge on Tractor button of a File Cache.

The run lives in the node's Send folder, and the node itself is left alone: a
wedge chooses values, and Review Wedges applies the chosen ones.
"""

from __future__ import annotations

import logging
import shutil
import traceback
from pathlib import Path

import hou
import tractor.api.author as author
from pxr import UsdGeom

from pipe.dcc.houdini import runtime
from pipe.dcc.houdini.tractor import (
    SendRefused,
    caches,
    commands,
    job,
    wedge_dialog,
    wedges,
)
from pipe.dcc.houdini.tractor.cache_send import (
    BUILD_FAILED,
    SNAPSHOT,
    check,
    claim,
    environment,
    note,
    refuse,
    spool,
    title,
    write_snapshot,
)
from pipe.dcc.houdini.util.nodetypes import FILE_CACHE

# The line of a node's comment that names its last wedge run.
COMMENT = "Tractor wedges"
LOP_IMPORT_CAMERA = "lopimportcam"
OBJ_CAMERAS = ("cam", LOP_IMPORT_CAMERA)

log = logging.getLogger(__name__)

# The rows last sent for each node this session, so the next round starts from them.
_last_rows: dict[str, list[wedge_dialog.Row]] = {}


def send(node: hou.Node) -> None:
    """Ask what to wedge, then spool one job that sims and flipbooks each wedge."""
    try:
        prepared = _prepare(node)
    except SendRefused as refusal:
        refuse(str(refusal))
        return
    except Exception:
        log.exception("Building the wedge job for %s failed", node.path())
        refuse(BUILD_FAILED, traceback.format_exc())
        return
    if prepared is None:
        return
    built, run = prepared

    job_id = spool(built, run.title)
    if job_id is None:
        note(node, COMMENT, f"{COMMENT} {run.folder.name}: not confirmed by Tractor")
        return
    hou.ui.displayMessage(
        f"Job {job_id} sims {len(run.wedges)} wedges of {node.path()} into\n"
        f"{run.folder}\n\nPress Review Wedges to play the finished ones.",
        title="Job sent to Tractor",
    )
    note(node, COMMENT, f"{COMMENT} {run.folder.name}: job {job_id}")


def _prepare(node: hou.Node) -> tuple[author.Job, wedges.Run] | None:
    """The job and the run it sims, or None when cancelled."""
    check(node)
    cameras = _cameras(node)
    start, end = (int(v) for v in node.evalParmTuple("f")[:2])
    choice = wedge_dialog.prompt(
        runtime.get_main_qt_window(),
        node,
        title(node, " wedges"),
        (start, end),
        cameras,
        _last_rows.get(node.path(), []),
        lambda rows: _wedges(node, rows),
    )
    if choice is None:
        return None
    _last_rows[node.path()] = choice.rows
    folder = claim(node)
    try:
        hip = Path(hou.hipFile.path())
        run = wedges.Run(
            title=choice.title,
            snapshot=folder / f"{SNAPSHOT}{hip.suffix}",
            hip=hip,
            node=node.path(),
            start=choice.start,
            end=choice.end,
            fps=hou.fps(),
            camera=choice.camera,
            wedges=choice.wedges,
        )
        spec = commands.Wedges(wedges.write_record(run), environment())
        names = [wedge.name for wedge in run.wedges]
        built = job.wedge_job(
            choice.title, choice.priority, choice.service, spec, names
        )
        write_snapshot(run.snapshot)
    except BaseException:
        # Nothing has read the folder yet.
        shutil.rmtree(folder)
        raise
    return built, run


def _cameras(node: hou.Node) -> list[wedges.Camera]:
    """What the flipbook can look through, the default first. A cache inside
    the stage is drawn from the stage, and one in /obj from /obj."""
    lop = next((n for n in _ancestors(node) if isinstance(n, hou.LopNode)), None)
    if lop is None:
        return _obj_cameras()
    # A LOP's parent is a LOP network, which has a display node.
    display = lop.parent().displayNode()  # ty: ignore[unresolved-attribute]
    if not isinstance(display, hou.LopNode):
        raise SendRefused(f"{lop.parent().path()} has no display node to flipbook.")
    paths = [
        str(prim.GetPath())
        for prim in display.stage().Traverse()
        if prim.IsA(UsdGeom.Camera)
    ]
    if not paths:
        raise SendRefused(
            f"The stage at {display.path()} has no camera to flipbook the wedges "
            "through. Load the shot camera, then Send again."
        )
    paths.sort(key=lambda path: path != wedges.SHOT_CAMERA)
    return [wedges.Camera(path, display.path()) for path in paths]


def _ancestors(node: hou.Node) -> list[hou.Node]:
    found = []
    parent = node.parent()
    while parent is not None:
        found.append(parent)
        parent = parent.parent()
    return found


def _obj_cameras() -> list[wedges.Camera]:
    # A LOP Import Camera brings in the shot camera, and the viewport's camera
    # is the one the artist flipbooks through.
    viewer = hou.ui.paneTabOfType(hou.paneTabType.SceneViewer)
    looking = (
        viewer.curViewport().camera() if isinstance(viewer, hou.SceneViewer) else None
    )
    found = [
        node
        for node in hou.node(
            "/obj"
        ).allSubChildren()  # ty: ignore[unresolved-attribute]
        if isinstance(node, hou.ObjNode) and node.type().name() in OBJ_CAMERAS
    ]
    if not found:
        raise SendRefused(
            "There is no camera in /obj to flipbook the wedges through. Add a "
            "LOP Import Camera of the shot camera, then Send again."
        )
    found.sort(key=lambda n: (n.type().name() != LOP_IMPORT_CAMERA, n != looking))
    return [wedges.Camera(n.path()) for n in found]


def _wedges(node: hou.Node, rows: list[wedge_dialog.Row]) -> list[wedges.Wedge]:
    """Check what the dialog's rows wedge, and make one wedge per combination."""
    parsed: list[tuple[str, list[wedges.Value]]] = []
    for path, text in rows:
        parm = _parm(path)
        if parm.path() in (done for done, _ in parsed):
            raise SendRefused(f"{wedges.label(parm.path())} is wedged twice.")
        try:
            values = wedges.parse(parm, text)
        except ValueError as error:
            raise SendRefused(str(error)) from None
        _check_reach(node, parm)
        parsed.append((parm.path(), values))
    return wedges.combine(parsed)


def _parm(path: str) -> hou.Parm:
    if not path:
        raise SendRefused("Choose the parm each row of values is for.")
    parm = hou.parm(path)
    if parm is not None:
        if parm.node().isInsideLockedHDA():
            raise SendRefused(f"{path} is inside a locked asset, so it can't be set.")
        return parm
    parms = hou.parmTuple(path)
    if parms is not None:
        raise SendRefused(
            f"{path} has {len(parms)} parts. Wedge one of them, such as "
            f"{parms[0].name()}."
        )
    raise SendRefused(f"There is no parm {path}.")


def _check_reach(node: hou.Node, parm: hou.Parm) -> None:
    """Refuse a parm whose wedges would all come back the same."""

    def loads(other: hou.Node) -> bool:
        return (
            other != node
            and other.type().name() == FILE_CACHE
            and bool(other.evalParm(caches.LOAD_FROM_DISK))
        )

    target = parm.node()
    if target in caches.reached(node, stop=loads):
        return
    name = wedges.label(parm.path())
    behind = [
        c for c in caches.reached(node) if loads(c) and target in caches.reached(c)
    ]
    if behind:
        raise SendRefused(
            f"{name} reaches {node.name()} only through {behind[0].path()}, which "
            "loads from disk, so every wedge would come back the same. Turn off "
            "its Load from Disk, then Send again."
        )
    raise SendRefused(
        f"{node.name()} doesn't read {name}, so every wedge would come back the same."
    )
