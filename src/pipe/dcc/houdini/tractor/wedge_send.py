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

from pipe.core.publish import Refused
from pipe.dcc.houdini import runtime
from pipe.dcc.houdini.publish import version
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

# The line of a node's comment that names its last wedge run.
COMMENT = "Tractor wedges"

log = logging.getLogger(__name__)

# The rows last sent for each node this session, so the next round starts from them.
_last_rows: dict[str, list[wedge_dialog.Row]] = {}


def send(node: hou.Node) -> None:
    """Ask what to wedge. The dialog's Send spools the job."""
    try:
        check(node)
        publish = _publish()
    except SendRefused as refusal:
        refuse(str(refusal))
        return
    path = node.path()
    start, end = (int(v) for v in node.evalParmTuple("f")[:2])
    wedge_dialog.show(
        runtime.get_main_qt_window(),
        node,
        title(node, " wedges"),
        (start, end),
        _last_rows.get(path, []),
        lambda parms, draw: _written(
            _node(path), publish, [p.node() for p in parms], draw
        ),
        lambda rows: _wedges(_node(path), rows),
        lambda choice: _submit(path, choice),
    )


def _submit(path: str, choice: wedge_dialog.WedgeChoice) -> None:
    """Spool one job that sims and flipbooks each wedge."""
    _last_rows[path] = choice.rows
    try:
        node = _node(path)
        check(node)
        publish = _publish()
        parms = [_parm(path).node() for path in choice.wedges[0].values]
        written = _written(node, publish, parms, choice.draw)
        built, run = _build(node, publish, choice, written)
    except SendRefused as refusal:
        refuse(str(refusal))
        return
    except Exception:
        log.exception("Building the wedge job for %s failed", path)
        refuse(BUILD_FAILED, traceback.format_exc())
        return

    job_id = spool(built, run.title)
    if job_id is None:
        note(node, COMMENT, f"{COMMENT} {run.folder.name}: not confirmed by Tractor")
        return
    what = f"flipbooks {len(run.wedges)} wedges of {node.name()}"
    if written == [node]:
        what = f"sims {len(run.wedges)} wedges of {node.name()}"
    elif written:
        names = ", ".join(cache.name() for cache in written)
        what = f"writes {names} for each of {len(run.wedges)} wedges of {node.name()}"
    hou.ui.displayMessage(
        f"Job {job_id} {what} into\n{run.folder}\n\n"
        "Press Review Wedges to play the finished ones.",
        title="Job sent to Tractor",
    )
    note(node, COMMENT, f"{COMMENT} {run.folder.name}: job {job_id}")


def _node(path: str) -> hou.Node:
    # The dialog stays open while the artist works, who may delete or rename it.
    node = hou.node(path)
    if node is None:
        raise SendRefused(f"{path} is gone. Press Wedge on Tractor on the node again.")
    return node


def _written(
    node: hou.Node, publish: hou.LopNode, parms: list[hou.Node], draw: str
) -> list[hou.Node]:
    """What each wedge writes, in cook order: every cache loading from disk
    between a wedged parm and what the flipbook draws, whose files would hide
    the change, and the node itself once a wedged parm reaches it."""
    if not parms:
        return []
    drawn = publish if draw == wedges.SHOT else node
    written = caches.between(parms, drawn)
    reached = caches.reached(node)
    if node in written or not any(parm in reached for parm in parms):
        return written
    # Before the first cache that reads it, which keeps the cook order.
    readers = [i for i, cache in enumerate(written) if node in caches.reached(cache)]
    written.insert(readers[0] if readers else len(written), node)
    return written


def _build(
    node: hou.Node,
    publish: hou.LopNode,
    choice: wedge_dialog.WedgeChoice,
    written: list[hou.Node],
) -> tuple[author.Job, wedges.Run]:
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
            publish=publish.path(),
            draw=choice.draw,
            caches=[_cache(cache, choice.end) for cache in written],
            wedges=choice.wedges,
        )
        paths = [cache.path for cache in run.caches]
        spec = commands.Wedges(
            wedges.write_record(run), environment(), wedges.chunks(run), paths
        )
        names = [wedge.name for wedge in run.wedges]
        # Made now, so Review's Delete tells a wedge yet to run from a deleted one.
        for name in names:
            (folder / name).mkdir()
        built = job.wedge_job(
            choice.title, choice.priority, choice.service, spec, names
        )
        write_snapshot(run.snapshot)
    except BaseException:
        # Nothing has read the folder yet.
        shutil.rmtree(folder)
        raise
    return built, run


def _cache(node: hou.Node, end: int) -> wedges.Cache:
    """The cache's own frames, up to the run's last."""
    chunks = caches.chunks(node)
    return wedges.Cache(node.path(), chunks[0][0], min(chunks[-1][1], float(end)))


def _publish() -> hou.LopNode:
    """The hip's publish node, whose stage every flipbook looks through the
    shot camera of."""
    try:
        publish = version.publish_node()
    except Refused as refusal:
        raise SendRefused(str(refusal)) from None
    if not publish.stage().GetPrimAtPath(wedges.SHOT_CAMERA):
        raise SendRefused(
            f"The stage at {publish.path()} has no {wedges.SHOT_CAMERA} to flipbook "
            "the wedges through. Load the shot camera, then Send again."
        )
    return publish


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
