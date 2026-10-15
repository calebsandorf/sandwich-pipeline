"""Stock File Cache 2.0 nodes as the farm sees them: the button the site adds,
the caches upstream of one, and the frames each of its tasks writes."""

from __future__ import annotations

import hou

from pipe.dcc.houdini.tractor.cache import ROP, STEP_TOLERANCE
from pipe.dcc.houdini.util.nodetypes import FILE_CACHE

BUTTON = "skd_cache_send"
BUTTON_LABEL = "Save to Disk on Tractor"
BUTTON_CALLBACK = (
    "from pipe.dcc.houdini.tractor import cache_send; cache_send.send(kwargs['node'])"
)
# Save to Disk's row ends with Cancel Cook, so the button starts the row below.
BUTTON_AFTER = "dirtyall"

LOAD_FROM_DISK = "loadfromdisk"
SIMULATION = "cachesim"
# The networks whose output is their display node. A VOP or ROP network has
# none, and the /stage network's is the whole stage.
DISPLAYED = (hou.ObjNode, hou.SopNode, hou.DopNode, hou.LopNode)
# Frames per task of a cache that is not a simulation. Each task loads the hip
# again, so a chunk is several frames rather than one.
CHUNK = 10


def add_button(node: hou.Node) -> None:
    """Run by the site's event scripts when a File Cache is created or loaded."""
    # A copied node brings its button along, and adding it twice raises.
    if node.parm(BUTTON) is not None or node.isInsideLockedHDA():
        return
    group = node.parmTemplateGroup()
    button = hou.ButtonParmTemplate(
        BUTTON,
        BUTTON_LABEL,
        script_callback=BUTTON_CALLBACK,
        script_callback_language=hou.scriptLanguage.Python,
        help="Write this cache on the farm, from a copy of the scene as it is now.",
    )
    group.insertAfter(BUTTON_AFTER, button)
    node.setParmTemplateGroup(group)


def sendable(node: hou.Node) -> bool:
    """A cache inside a locked asset can't be set to load from disk, so it is
    never offered as Cache first."""
    return node.type().name() == FILE_CACHE and not node.isInsideLockedHDA()


def upstream(node: hou.Node) -> list[hou.Node]:
    """The sendable File Caches `node` reads from, each after the caches it reads."""
    found: list[hou.Node] = []
    seen = {(node.sessionId(), True)}
    stack = [(node, iter(_feeds(node, True, node)))]
    while stack:
        current, feeds = stack[-1]
        nxt = next(feeds, None)
        if nxt is None:
            stack.pop()
            if current != node and sendable(current) and current not in found:
                found.append(current)
            continue
        if (nxt[0].sessionId(), nxt[1]) not in seen:
            seen.add((nxt[0].sessionId(), nxt[1]))
            stack.append((nxt[0], iter(_feeds(nxt[0], nxt[1], node))))
    return found


def _feeds(node: hou.Node, whole: bool, sent: hou.Node) -> list[tuple[hou.Node, bool]]:
    """What `node` cooks from, each with whether to follow it whole."""
    inputs = [n for n in node.inputs() if n is not None]
    if not whole:
        return [(n, not _within(sent, n)) for n in inputs]
    found = inputs + list(node.references(include_children=False))
    display = node.displayNode() if isinstance(node, DISPLAYED) else None
    if display is not None:
        found.append(display)
    return [(n, not (_within(node, n) or _within(sent, n))) for n in found]


def _within(node: hou.Node, network: hou.Node) -> bool:
    return node == network or node.path().startswith(network.path() + "/")


def chunks(node: hou.Node) -> list[tuple[float, float]]:
    """The start and end frame of each task that writes `node`.

    A simulation is one task, since each frame cooks from the last. A cache
    that is not time dependent writes the scene's current frame.
    """
    rop = node.node(ROP)
    if rop is None or not rop.evalParm("trange"):
        return [(hou.frame(), hou.frame())]
    start, end, step = (float(v) for v in node.evalParmTuple("f"))
    if node.evalParm(SIMULATION):
        return [(start, end)]
    # The ROP's own increment, which is smaller than the node's with substeps.
    increment = float(rop.evalParm("f3"))
    count = int((end - start) / step + STEP_TOLERANCE) + 1
    frames = [start + i * step for i in range(count)]
    found = []
    for i in range(0, len(frames), CHUNK):
        first, last = frames[i], frames[min(i + CHUNK, len(frames)) - 1]
        # A chunk also writes the substeps up to the next chunk's first frame.
        found.append((first, end if last == frames[-1] else last + step - increment))
    return found
