"""Cache Send: the Save to Disk on Tractor button of a File Cache."""

from __future__ import annotations

import logging
import shutil
import traceback
from pathlib import Path

import hou
import tractor.api.author as author

from pipe.core.cache import SIM_DIRNAME, link_to_cache
from pipe.core.render import next_version
from pipe.core.util.paths import get_production_path
from pipe.dcc.houdini import launch, runtime
from pipe.dcc.houdini.tractor import SendRefused, cache_dialog, caches, commands, job
from pipe.dcc.houdini.tractor.send import ENV_VARS

SEND_FOLDER = ".tractor"
SNAPSHOT = "scene"
# Set by Open Shot for the substep sync HDA, and never saved in the hip.
SHOT_VARS = ("SHOT_SUBSTEPS",)
# The line of a node's comment that names its last job.
COMMENT = "Tractor job"

log = logging.getLogger(__name__)


def send(node: hou.Node) -> None:
    """Ask how to send `node`, then spool one job that writes it."""
    try:
        prepared = _prepare(node)
    except SendRefused as refusal:
        _refuse(str(refusal))
        return
    except Exception:
        log.exception("Building the job for %s failed", node.path())
        _refuse(
            "The job could not be built, so nothing was sent to Tractor. The "
            "details below say what went wrong.",
            traceback.format_exc(),
        )
        return
    if prepared is None:
        return
    built, title, sent = prepared

    try:
        job_id = built.spool(block=True)
    except Exception:
        # A lost reply looks the same as a refusal, and the job may already be
        # queued and reading its snapshot, so it stays.
        log.exception("Tractor did not confirm the job for %s", node.path())
        job_id = None
        hou.ui.displayMessage(
            "Tractor did not confirm the job, but it may still have been queued. "
            f'Look for "{title}" in Tractor before sending again.',
            title="Tractor did not confirm the job",
            severity=hou.severityType.Warning,
            details=traceback.format_exc(),
        )
    else:
        hou.ui.displayMessage(
            f"Job {job_id} caches:\n"
            + "\n".join(cache.path() for cache in sent)
            + "\n\nCheck its progress in Tractor.",
            title="Job sent to Tractor",
        )
    for cache in sent:
        _record(cache, job_id)


def _prepare(node: hou.Node) -> tuple[author.Job, str, list[hou.Node]] | None:
    """The job, its title and the caches it writes, or None when cancelled."""
    if hou.hipFile.isNewFile():
        raise SendRefused("Save the hip file first. Its caches are written beside it.")
    # A cache saved into an asset keeps its button there.
    if node.isInsideLockedHDA():
        raise SendRefused(
            f"{node.path()} is inside the locked asset {_locked_asset(node).path()}, "
            "so Send can't turn on its Load from Disk. Choose Allow Editing of "
            "Contents on the asset, then Send again."
        )
    choice = cache_dialog.prompt(
        runtime.get_main_qt_window(),
        node,
        caches.upstream(node),
        _title(node),
        _frames(node),
    )
    if choice is None:
        return None
    sent = [*choice.first, node]
    folder = _claim(node)
    try:
        hip = Path(hou.hipFile.path())
        snapshot = folder / f"{SNAPSHOT}{hip.suffix}"
        env = _environment()
        specs = [
            commands.Cache(snapshot, hip, cache.path(), caches.chunks(cache), env)
            for cache in sent
        ]
        built = job.cache_job(choice.title, choice.priority, choice.service, specs)
        _load_from_disk(choice.first, node)
        _write_snapshot(snapshot)
    except BaseException:
        # Nothing has read the folder yet.
        shutil.rmtree(folder)
        raise
    return built, choice.title, sent


def _refuse(message: str, details: str | None = None) -> None:
    hou.ui.displayMessage(
        message,
        title="Nothing was sent to Tractor",
        severity=hou.severityType.Error,
        details=details,
    )


def _locked_asset(node: hou.Node) -> hou.Node:
    asset = node.parent()
    while not asset.isLockedHDA():
        asset = asset.parent()
    return asset


def _title(node: hou.Node) -> str:
    # The last folders of a shot's fx hip are <shot>/fx.
    hip = Path(hou.text.expandString("$HIP"))
    return f"{hip.parent.name} {hip.name}: {node.name()}"


def _frames(node: hou.Node) -> str:
    chunks = caches.chunks(node)
    start, end = chunks[0][0], chunks[-1][1]
    return f"{start:g}" if start == end else f"{start:g} to {end:g}"


def _claim(node: hou.Node) -> Path:
    """Creates a folder on disk: the next version of the node's Send folder."""
    geo = Path(hou.text.expandString("$HIP")) / SIM_DIRNAME
    # Only Open Shot links a hip's geo folder to the cache mount.
    if geo.is_relative_to(get_production_path()):
        link_to_cache(geo)
    folder = next_version(geo / SEND_FOLDER / node.name())
    try:
        folder.mkdir(parents=True)
    except OSError as error:
        raise SendRefused(f"Could not create {folder}: {error.strerror}.") from None
    return folder


def _environment() -> dict[str, str]:
    """The session's values of what a blade needs to load the hip as it did."""
    names = {name for name, value in launch.environment().items() if value is not None}
    names.update(ENV_VARS, SHOT_VARS)
    # hou.getenv sees what hou.putenv set, which os.environ doesn't.
    return {name: value for name in sorted(names) if (value := hou.getenv(name))}


def _load_from_disk(first: list[hou.Node], node: hou.Node) -> None:
    """What Save to Disk does after writing, done before the snapshot so the
    sent cache reads what the Cache first caches write."""
    # File Cache's own Python module, which its Save to Disk button calls.
    for cache in [*first, node]:
        cache.hdaModule().hardenBaseName(cache)
    for cache in first:
        cache.setParms({caches.LOAD_FROM_DISK: 1})
    node.hdaModule().enableLoadFromDisk(node)


def _write_snapshot(path: Path) -> None:
    """Writes the session to `path`, unsaved edits included.

    Unlike saving, `mwrite -n` leaves the session's file name and `$HIP` alone.
    """
    _, error = hou.hscript(f'mwrite -n "{path}"')
    if error or not path.is_file():
        raise SendRefused(f"Could not write the scene to {path}.\n\n{error}")


def _record(node: hou.Node, job_id: int | None) -> None:
    """Name the job in the node's comment, keeping the artist's own lines."""
    if job_id is None:
        line = f"{COMMENT} not confirmed by Tractor"
    else:
        line = f"{COMMENT} {job_id}"
    kept = [
        text for text in node.comment().splitlines() if not text.startswith(COMMENT)
    ]
    node.setComment("\n".join([*kept, line]))
    node.setGenericFlag(hou.nodeFlag.DisplayComment, True)
