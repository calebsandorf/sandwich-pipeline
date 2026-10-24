"""Cache Send: the Save to Disk on Tractor button of a File Cache."""

from __future__ import annotations

import logging
import shutil
import traceback
from pathlib import Path

import hou
import tractor.api.author as author

from pipe.dcc.houdini import runtime
from pipe.dcc.houdini.tractor import (
    SendRefused,
    cache_dialog,
    caches,
    commands,
    job,
    snapshot,
)
from pipe.dcc.houdini.tractor.send import environment

# The line of a node's comment that names its last job.
COMMENT = "Tractor job"
# File Cache's file name before the version, which Save to Disk hardens.
BASE_NAME = "basename"
BUILD_FAILED = (
    "The job could not be built, so nothing was sent to Tractor. The details "
    "below say what went wrong."
)

log = logging.getLogger(__name__)


def send(node: hou.Node) -> None:
    """Ask how to send `node`, then spool one job that writes it."""
    try:
        prepared = _prepare(node)
    except SendRefused as refusal:
        refuse(str(refusal))
        return
    except Exception:
        log.exception("Building the job for %s failed", node.path())
        refuse(BUILD_FAILED, traceback.format_exc())
        return
    if prepared is None:
        return
    built, title, sent = prepared

    job_id = spool(
        built,
        title,
        "Its caches still cook live. Turn on Load from Disk once it has "
        "written them.",
    )
    if job_id is not None:
        _load_from_disk(sent)
        hou.ui.displayMessage(
            f"Job {job_id} caches:\n"
            + "\n".join(cache.path() for cache in sent)
            + "\n\nCheck its progress in Tractor.",
            title="Job sent to Tractor",
        )
    for cache in sent:
        note(cache, COMMENT, _job_line(job_id))


def spool(built: author.Job, title: str, unconfirmed: str = "") -> int | None:
    """The job's id, or None after warning that Tractor did not confirm it,
    ending with `unconfirmed`."""
    try:
        return built.spool(block=True)
    except Exception:
        # A lost reply looks the same as a refusal, and the job may already be
        # queued and reading its snapshot, so its folder stays.
        log.exception("Tractor did not confirm the job %s", title)
        hou.ui.displayMessage(
            "Tractor did not confirm the job, but it may still have been queued. "
            f'Look for "{title}" in Tractor before sending again.\n\n'
            f"{unconfirmed}".strip(),
            title="Tractor did not confirm the job",
            severity=hou.severityType.Warning,
            details=traceback.format_exc(),
        )
        return None


def _job_line(job_id: int | None) -> str:
    if job_id is None:
        return f"{COMMENT} not confirmed"
    return f"{COMMENT} {job_id}"


def _prepare(node: hou.Node) -> tuple[author.Job, str, list[hou.Node]] | None:
    """The job, its title and the caches it writes, or None when cancelled."""
    check(node)
    choice = cache_dialog.prompt(
        runtime.get_main_qt_window(),
        node,
        caches.upstream(node),
        title(node),
        _frames(node),
    )
    if choice is None:
        return None
    sent = [*choice.first, node]
    folder = snapshot.claim(node)
    try:
        hip = Path(hou.hipFile.path())
        scene = folder / f"{snapshot.SNAPSHOT}{hip.suffix}"
        env = environment()
        specs = [
            commands.Cache(scene, hip, cache.path(), caches.chunks(cache), env)
            for cache in sent
        ]
        built = job.cache_job(choice.title, choice.priority, choice.service, specs)
        _write_loading(scene, sent)
    except BaseException:
        # Nothing has read the folder yet.
        shutil.rmtree(folder)
        raise
    return built, choice.title, sent


def refuse(message: str, details: str | None = None) -> None:
    hou.ui.displayMessage(
        message,
        title="Nothing was sent to Tractor",
        severity=hou.severityType.Error,
        details=details,
    )


def check(node: hou.Node) -> None:
    """Refuse a node no Send can write."""
    if hou.hipFile.isNewFile():
        raise SendRefused("Save the hip file first. Its caches are written beside it.")
    # A cache saved into an asset keeps its buttons there.
    if node.isInsideLockedHDA():
        raise SendRefused(
            f"{node.path()} is inside the locked asset {_locked_asset(node).path()}, "
            "so Send can't change its parms. Choose Allow Editing of Contents on "
            "the asset, then Send again."
        )


def _locked_asset(node: hou.Node) -> hou.Node:
    asset = node.parent()
    while not asset.isLockedHDA():
        asset = asset.parent()
    return asset


def title(node: hou.Node, what: str = "") -> str:
    # The last folders of a shot's fx hip are <shot>/fx.
    hip = Path(hou.text.expandString("$HIP"))
    return f"{hip.parent.name} {hip.name}: {node.name()}{what}"


def _frames(node: hou.Node) -> str:
    chunks = caches.chunks(node)
    start, end = chunks[0][0], chunks[-1][1]
    return f"{start:g}" if start == end else f"{start:g} to {end:g}"


def _write_loading(scene: Path, sent: list[hou.Node]) -> None:
    """Writes the snapshot as Save to Disk would leave the sent caches, so
    each reads what the ones before it write. The live caches load from disk
    only once Tractor confirms the job."""
    # Every File Cache has both. The stubs say parm() may be None.
    parms: list[hou.Parm] = [  # ty: ignore[invalid-assignment]
        cache.parm(name)
        for cache in sent
        for name in (BASE_NAME, caches.LOAD_FROM_DISK)
    ]
    # Keyframes and expressions included. The stubs leave out asData and
    # setFromData.
    kept = [parm.asData() for parm in parms]  # ty: ignore[unresolved-attribute]
    # Out of the undo list, since the live scene ends up as it was.
    with hou.undos.disabler():
        _load_from_disk(sent)
        try:
            snapshot.write(scene)
        finally:
            for parm, data in zip(parms, kept, strict=True):
                parm.setFromData(data)  # ty: ignore[unresolved-attribute]


def _load_from_disk(sent: list[hou.Node]) -> None:
    """What Save to Disk does after writing. The last cache is the node sent,
    which keeps its own Load from Disk on Save choice."""
    # File Cache's own Python module, which its Save to Disk button calls.
    for cache in sent:
        cache.hdaModule().hardenBaseName(cache)
    for cache in sent[:-1]:
        cache.setParms({caches.LOAD_FROM_DISK: 1})
    sent[-1].hdaModule().enableLoadFromDisk(sent[-1])


def note(node: hou.Node, prefix: str, line: str) -> None:
    """Replace the line of the node's comment that starts with `prefix`,
    keeping the artist's own lines."""
    kept = [text for text in node.comment().splitlines() if not text.startswith(prefix)]
    node.setComment("\n".join([*kept, line]))
    node.setGenericFlag(hou.nodeFlag.DisplayComment, True)
