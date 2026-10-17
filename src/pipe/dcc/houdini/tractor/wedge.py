"""What a Wedge Send task runs on a blade: hython sims one wedge of a run,
flipbooks some of its frames, or finishes its flipbook once every frame is drawn.

    hython -m pipe.dcc.houdini.tractor.wedge <wedge.json> <wedge> sim
    hython -m pipe.dcc.houdini.tractor.wedge <wedge.json> <wedge> flipbook <start> <end>
    hython -m pipe.dcc.houdini.tractor.wedge <wedge.json> <wedge> finish

Sim and flipbook set the wedge's values and point the cache into the wedge's
folder, so the flipbook draws the scene as the sim saw it, reading the wedge's cache.
The flipbook looks through the shot camera at the stage of the hip's publish node,
or at only the cache's output under that camera.
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
from pipe.dcc.houdini.tractor.commands import (
    WEDGE_DRAW_FAILED,
    WEDGE_FINISH,
    WEDGE_FLIPBOOK,
    WEDGE_SIM,
)

# The Flipbook ROP's Source menu.
LOP_SOURCE = 1
RAW = "raw"
SCRATCH = f"{wedges.FLIPBOOK}.tmp"


def main(argv: list[str] | None = None) -> int:
    args = _parse(argv)
    run = wedges.read_record(args.record)
    wedge = next(w for w in run.wedges if w.name == args.wedge)
    folder = run.folder / wedge.name
    if args.step == WEDGE_FINISH:
        return finish(run, folder)
    cache.load(run.snapshot, run.hip)
    missing = wedges.set_values(wedge)
    if missing:
        print(f"The snapshot has no parm {', '.join(missing)}.", file=sys.stderr)
        return 1
    node = hou.node(run.node)
    if node is None:
        print(f"The snapshot has no node {run.node}.", file=sys.stderr)
        return 1
    wedges.redirect(node, folder)
    if args.step == WEDGE_SIM:
        return cache.write(run.node, run.start, run.end)
    # The flipbook reads the cache the sim wrote, as the artist's viewport would.
    node.setParms({LOAD_FROM_DISK: 1})
    start, end = args.frames
    return flipbook(run, wedge, folder, start, end)


def _parse(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("record", type=Path, help="the run's wedge.json")
    parser.add_argument("wedge", help="the wedge's name in the record")
    parser.add_argument("step", choices=(WEDGE_SIM, WEDGE_FLIPBOOK, WEDGE_FINISH))
    parser.add_argument(
        "frames", type=int, nargs="*", help="a flipbook's start and end"
    )
    args = parser.parse_args(argv)
    if (args.step == WEDGE_FLIPBOOK) != (len(args.frames) == 2):
        parser.error("flipbook takes a start and end frame, and only flipbook does")
    return args


def flipbook(
    run: wedges.Run, wedge: wedges.Wedge, folder: Path, start: int, end: int
) -> int:
    """Draw `start` to `end` through the shot camera and burn the wedge's values on.

    The frames are written aside, for `finish` to rename into place, so review
    never lists a wedge whose flipbook is half written.
    """
    scratch = folder / SCRATCH
    # Its own folder, since ffmpeg would also burn the frames of other tasks.
    raw = scratch / f"{RAW}.{start}-{end}"
    shutil.rmtree(raw, ignore_errors=True)
    raw.mkdir(parents=True)
    rop = _flipbook_rop(run)
    if rop is None:
        return 1
    rop.setParms({"picture": str(raw / f"{RAW}.$F4.png")})
    try:
        rop.render(frame_range=(start, end), output_progress=True)
    except RuntimeError as error:
        print(f"The flipbook couldn't draw: {error}", file=sys.stderr)
        return WEDGE_DRAW_FAILED

    frames = range(start, end + 1)
    missing = [f for f in frames if not (raw / f"{RAW}.{f:04}.png").is_file()]
    if missing:
        print(f"The flipbook wrote no frame {missing[0]}.", file=sys.stderr)
        return 1
    width, height = hou.imageResolution(str(raw / f"{RAW}.{start:04}.png"))
    burn_hud_frames(
        str(raw / f"{RAW}.%04d.png"),
        str(scratch / f"{wedges.FRAMES}.%04d.png"),
        _hud(run, wedge, start),
        (width, height),
        start_frame=start,
    )
    shutil.rmtree(raw)
    print(f"Flipbooked frames {start}-{end} of {wedge.name}.")
    return 0


def finish(run: wedges.Run, folder: Path) -> int:
    """Rename the drawn frames into place once every one is there."""
    scratch = folder / SCRATCH
    frames = range(run.start, run.end + 1)
    for frame in frames:
        if not (scratch / f"{wedges.FRAMES}.{frame:04}.png").is_file():
            print(f"The flipbook has no frame {frame}.", file=sys.stderr)
            return 1
    final = folder / wedges.FLIPBOOK
    shutil.rmtree(final, ignore_errors=True)
    scratch.rename(final)
    print(f"Flipbooked {len(frames)} frames into {final}.")
    return 0


def _flipbook_rop(run: wedges.Run) -> hou.RopNode | None:
    """A Flipbook ROP that draws what the run chose, or None after saying why it
    can't."""
    publish = hou.node(run.publish)
    if not isinstance(publish, hou.LopNode):
        print(f"The snapshot has no publish node {run.publish}.", file=sys.stderr)
        return None
    drawn = publish if run.draw == wedges.SHOT else _cache_only(publish, run.node)
    # A missing camera prim draws a blank frame without an error.
    if not drawn.stage().GetPrimAtPath(wedges.SHOT_CAMERA):
        print(
            f"The stage of {run.publish} has no camera {wedges.SHOT_CAMERA}.",
            file=sys.stderr,
        )
        return None
    # The viewport's renderer. The OpenGL ROP reloads every texture each frame,
    # which made a textured set minutes per frame.
    rop = hou.node("/out").createNode("flipbook")  # ty: ignore[unresolved-attribute]
    rop.setParms(
        {
            "opsource": LOP_SOURCE,
            "loppath": drawn.path(),
            "cameraprim": wedges.SHOT_CAMERA,
            "trange": 1,
            "f3": 1,
        }
    )
    return rop  # ty: ignore[invalid-return-type]


def _cache_only(publish: hou.LopNode, node: str) -> hou.LopNode:
    """A stage of the cache's output alone, under the publish node's shot camera.

    The publish node has no output, so the camera is kept from its input.
    """
    stage = publish.parent()
    camera = stage.createNode("prune")
    camera.setInput(0, publish.input(0))
    camera.setParms(
        {
            "primpattern1": wedges.SHOT_CAMERA,
            "pruneunselected": 1,
            "method": "deactivate",
        }
    )
    # SOP Import draws the geometry where the SOP has it, without any transform
    # of the object around it. Sims are made in world space.
    geometry = stage.createNode("sopimport")
    geometry.setParms({"soppath": node})
    merge = stage.createNode("merge")
    merge.setInput(0, camera)
    merge.setInput(1, geometry)
    return merge  # ty: ignore[invalid-return-type]


def _hud(run: wedges.Run, wedge: wedges.Wedge, start: int) -> HudContent:
    # Rows stack bottom-up, so the values read top-down in the record's order.
    values = tuple(reversed(wedges.describe(wedge)))
    name = f"{run.title} {run.folder.name} {wedge.name}"
    return HudContent(left_lines=(*values, name), frame_start=start)


if __name__ == "__main__":
    sys.exit(main())
