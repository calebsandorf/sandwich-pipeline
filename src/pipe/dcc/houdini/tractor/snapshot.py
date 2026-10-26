"""The snapshot of the hip a cache or wedge Send writes, and how a blade loads it.

Blades import this module, so it imports nothing only the artist's session has.
"""

from __future__ import annotations

import sys
from pathlib import Path

import hou

from pipe.core.cache import SIM_DIRNAME, link_to_cache
from pipe.core.render import next_version
from pipe.core.util.paths import get_production_path
from pipe.dcc.houdini.tractor import SendRefused

SEND_FOLDER = ".tractor"
SNAPSHOT = "scene"


def folder(node: hou.Node) -> Path:
    """The node's Send folder, which holds a version for each Send."""
    return _geo() / SEND_FOLDER / node.name()


def _geo() -> Path:
    return Path(hou.text.expandString("$HIP")) / SIM_DIRNAME


def claim(node: hou.Node) -> Path:
    """Creates a folder on disk: the next version of the node's Send folder."""
    # Only Open Shot links a hip's geo folder to the cache mount.
    if _geo().is_relative_to(get_production_path()):
        link_to_cache(_geo())
    version = next_version(folder(node))
    try:
        version.mkdir(parents=True)
    except OSError as error:
        raise SendRefused(f"Could not create {version}: {error.strerror}.") from None
    return version


def write(path: Path) -> None:
    """Writes the session to `path`, unsaved edits included.

    Unlike saving, `mwrite -n` leaves the session's file name and `$HIP` alone.
    It does mark the hip modified, and nothing clears that but a save, so every
    Send leaves the hip modified.
    """
    _, error = hou.hscript(f'mwrite -n "{path}"')
    if error or not path.is_file():
        raise SendRefused(f"Could not write the scene to {path}.\n\n{error}")


def load(snapshot: Path, hip: Path) -> None:
    """Load the snapshot as if it were the artist's hip."""
    try:
        hou.hipFile.load(str(snapshot), suppress_save_prompt=True)
    except hou.LoadWarning as warning:
        # The scene did load; the artist's own session showed the same warnings.
        print(warning, file=sys.stderr)
    # The snapshot sits under the hip's geo folder, so loading it moved $HIP and
    # $HIPNAME, which every default cache path is built from.
    hou.hipFile.setName(str(hip))
