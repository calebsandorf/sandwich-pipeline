"""What a Wedge Send task runs on a blade: hython sims one wedge of a run, or
flipbooks it, from the run's snapshot.

    hython -m pipe.dcc.houdini.tractor.wedge <wedge.json> <wedge> sim|flipbook

Both steps set the wedge's values and point the cache into the wedge's folder,
so the flipbook draws the scene as the sim saw it, reading the wedge's cache.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

import hou

from pipe.core.hud import HudContent
from pipe.core.playblast.encoding import burn_hud_frames
from pipe.dcc.houdini.tractor import cache, wedges
from pipe.dcc.houdini.tractor.caches import LOAD_FROM_DISK
from pipe.dcc.houdini.tractor.commands import WEDGE_FLIPBOOK, WEDGE_SIM

# The Flipbook ROP's Source menu.
OBJ_SOURCE = 0
LOP_SOURCE = 1
RAW = "raw"


def main(argv: list[str] | None = None) -> int:
    args = _parse(argv)
    run = wedges.read_record(args.record)
    wedge = next(w for w in run.wedges if w.name == args.wedge)
    cache.load(run.snapshot, run.hip)
    missing = wedges.set_values(wedge)
    if missing:
        print(f"The snapshot has no parm {', '.join(missing)}.", file=sys.stderr)
        return 1
    node = hou.node(run.node)
    if node is None:
        print(f"The snapshot has no node {run.node}.", file=sys.stderr)
        return 1
    folder = run.folder / wedge.name
    wedges.redirect(node, folder)
    if args.step == WEDGE_SIM:
        return cache.write(run.node, run.start, run.end)
    # The flipbook reads the cache the sim wrote, as the artist's viewport would.
    node.setParms({LOAD_FROM_DISK: 1})
    return flipbook(run, wedge, folder)


def _parse(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("record", type=Path, help="the run's wedge.json")
    parser.add_argument("wedge", help="the wedge's name in the record")
    parser.add_argument("step", choices=(WEDGE_SIM, WEDGE_FLIPBOOK))
    return parser.parse_args(argv)


def flipbook(run: wedges.Run, wedge: wedges.Wedge, folder: Path) -> int:
    """Draw every frame through the run's camera and burn the wedge's values on.

    The frames are written aside and the folder renamed into place, so review
    never lists a wedge whose flipbook is half written.
    """
    scratch = folder / f"{wedges.FLIPBOOK}.tmp"
    shutil.rmtree(scratch, ignore_errors=True)
    scratch.mkdir(parents=True)
    rop = _flipbook_rop(run.camera)
    if rop is None:
        return 1
    rop.setParms({"picture": str(scratch / f"{RAW}.$F4.png")})
    rop.render(frame_range=(run.start, run.end), output_progress=True)

    frames = range(run.start, run.end + 1)
    missing = [f for f in frames if not (scratch / f"{RAW}.{f:04}.png").is_file()]
    if missing:
        print(f"The flipbook wrote no frame {missing[0]}.", file=sys.stderr)
        return 1
    width, height = hou.imageResolution(str(scratch / f"{RAW}.{run.start:04}.png"))
    burn_hud_frames(
        str(scratch / f"{RAW}.%04d.png"),
        str(scratch / f"{wedges.FRAMES}.%04d.png"),
        _hud(run, wedge),
        (width, height),
        start_frame=run.start,
    )
    for frame in frames:
        (scratch / f"{RAW}.{frame:04}.png").unlink()
    final = folder / wedges.FLIPBOOK
    shutil.rmtree(final, ignore_errors=True)
    scratch.rename(final)
    print(f"Flipbooked {len(frames)} frames of {wedge.name} into {final}.")
    return 0


def _flipbook_rop(camera: wedges.Camera) -> hou.RopNode | None:
    """A Flipbook ROP that draws the scene as the run chose, or None after
    saying why it can't."""
    # The viewport's renderer. The OpenGL ROP reloads every texture each frame,
    # which made a textured set minutes per frame.
    rop = hou.node("/out").createNode("flipbook")  # ty: ignore[unresolved-attribute]
    if camera.lop is None:
        if hou.node(camera.path) is None:
            print(f"The snapshot has no camera {camera.path}.", file=sys.stderr)
            return None
        rop.setParms({"opsource": OBJ_SOURCE, "camera": camera.path})
    else:
        lop = hou.node(camera.lop)
        # A missing camera prim draws a blank frame without an error.
        if not isinstance(lop, hou.LopNode) or not lop.stage().GetPrimAtPath(
            camera.path
        ):
            print(
                f"The stage of {camera.lop} has no camera {camera.path}.",
                file=sys.stderr,
            )
            return None
        rop.setParms(
            {"opsource": LOP_SOURCE, "loppath": camera.lop, "cameraprim": camera.path}
        )
    rop.setParms({"trange": 1, "f3": 1})
    return rop  # ty: ignore[invalid-return-type]


def _hud(run: wedges.Run, wedge: wedges.Wedge) -> HudContent:
    # Rows stack bottom-up, so the values read top-down in the record's order.
    values = tuple(reversed(wedges.describe(wedge)))
    name = f"{run.title} {run.folder.name} {wedge.name}"
    return HudContent(left_lines=(*values, name), frame_start=run.start)


if __name__ == "__main__":
    sys.exit(main())
