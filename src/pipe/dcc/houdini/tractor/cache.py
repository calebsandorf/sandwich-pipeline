"""What a Cache Send task runs on a blade: hython writes one File Cache's frames
from the Send's snapshot of the hip.

    hython -m pipe.dcc.houdini.tractor.cache <snapshot> <hip> <node> <start> <end>

The File Cache reads a missing or truncated frame as empty geometry with only a
warning, and a shorter save leaves an earlier save's frames behind. So the task
deletes the frames it is about to write, loads each one back after writing, and
exits non-zero when any does not load, which fails the task in Tractor.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import hou

from pipe.dcc.houdini.util.nodetypes import FILE_CACHE

# The ROP inside File Cache 2.0 that its Save to Disk button presses.
ROP = "render"
# A step is a float; this keeps the last frame in when float steps fall just short of it.
STEP_TOLERANCE = 1e-6


def main(argv: list[str] | None = None) -> int:
    args = _parse(argv)
    _load(args.snapshot, args.hip)
    rop = _rop(args.node)
    files = [_output(rop, frame) for frame in _frames(rop, args.start, args.end)]
    for file in files:
        file.unlink(missing_ok=True)
    if rop.evalParm("trange"):
        rop.render(frame_range=(args.start, args.end), output_progress=True)
    else:
        rop.render(output_progress=True)
    unreadable = [file for file in files if not _loads(file)]
    for file in unreadable:
        print(f"Not written, or not readable: {file}", file=sys.stderr)
    if unreadable:
        print(
            f"{len(unreadable)} of {len(files)} frames of {args.node} failed.",
            file=sys.stderr,
        )
        return 1
    print(f"Wrote {len(files)} frames of {args.node}.")
    return 0


def _parse(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path, help="the hip Send wrote for this job")
    parser.add_argument("hip", type=Path, help="the artist's hip the snapshot is of")
    parser.add_argument("node", help="the File Cache to write")
    parser.add_argument("start", type=float)
    parser.add_argument("end", type=float)
    return parser.parse_args(argv)


def _load(snapshot: Path, hip: Path) -> None:
    """Load the snapshot as if it were the artist's hip."""
    try:
        hou.hipFile.load(str(snapshot), suppress_save_prompt=True)
    except hou.LoadWarning as warning:
        # The scene did load; the artist's own session showed the same warnings.
        print(warning, file=sys.stderr)
    # The snapshot sits under the hip's geo folder, so loading it moved $HIP and
    # $HIPNAME, which every default cache path is built from.
    hou.hipFile.setName(str(hip))


def _rop(path: str) -> hou.RopNode:
    node = hou.node(path)
    if node is None or node.type().name() != FILE_CACHE:
        raise SystemExit(f"{path} is not a File Cache 2.0 node in the snapshot.")
    # Every File Cache 2.0 holds this ROP; hou types the child as any node.
    return node.node(ROP)  # ty: ignore[invalid-return-type]


def _parm(rop: hou.RopNode, name: str) -> hou.Parm:
    # The stock ROP always has the parms this module reads.
    return rop.parm(name)  # ty: ignore[invalid-return-type]


def _frames(rop: hou.RopNode, start: float, end: float) -> list[float]:
    """Every time the ROP writes a file for, substeps included."""
    if not rop.evalParm("trange"):
        return [hou.frame()]
    # The ROP's own increment: the node's step divided by its substeps.
    step = _parm(rop, "f3").evalAsFloat()
    count = int((end - start) / step + STEP_TOLERANCE)
    return [start + i * step for i in range(count + 1)]


def _output(rop: hou.RopNode, frame: float) -> Path:
    return Path(_parm(rop, "sopoutput").evalAsStringAtFrame(frame))


def _loads(file: Path) -> bool:
    try:
        hou.Geometry().loadFromFile(str(file))
    except hou.OperationFailed:
        return False
    return True


if __name__ == "__main__":
    sys.exit(main())
