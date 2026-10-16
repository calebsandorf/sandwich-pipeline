"""Stock File Cache 2.0 nodes as the farm sees them: the button the site adds,
the caches upstream of one, and the frames each of its tasks writes."""

from __future__ import annotations

from collections.abc import Callable

import hou

from pipe.dcc.houdini.tractor.cache import ROP, STEP_TOLERANCE
from pipe.dcc.houdini.util.nodetypes import FILE_CACHE

BUTTON = "skd_cache_send"
WEDGE_BUTTON = "skd_wedge_send"
REVIEW_BUTTON = "skd_wedge_review"
# Name, label, callback and help of each button, in the order they sit in one row.
BUTTONS = (
    (
        BUTTON,
        "Save to Disk on Tractor",
        "from pipe.dcc.houdini.tractor import cache_send; "
        "cache_send.send(kwargs['node'])",
        "Write this cache on the farm, from a copy of the scene as it is now.",
    ),
    (
        WEDGE_BUTTON,
        "Wedge on Tractor",
        "from pipe.dcc.houdini.tractor import wedge_send; "
        "wedge_send.send(kwargs['node'])",
        "Sim this cache on the farm once for each wedged value, and flipbook each.",
    ),
    (
        REVIEW_BUTTON,
        "Review Wedges",
        "from pipe.dcc.houdini.tractor import wedge_review; "
        "wedge_review.review(kwargs['node'])",
        "Play the finished wedges of this cache, and apply one's values.",
    ),
)
# Save to Disk's row ends with Cancel Cook, so the buttons start the row below.
BUTTON_AFTER = "dirtyall"

LOAD_FROM_DISK = "loadfromdisk"
SIMULATION = "cachesim"
# The networks whose output is their display node. A VOP or ROP network has
# none, and the /stage network's is the whole stage.
DISPLAYED = (hou.ObjNode, hou.SopNode, hou.DopNode, hou.LopNode)
# Frames per task of a cache that is not a simulation. Each task loads the hip
# again, so a chunk is several frames rather than one.
CHUNK = 10


def add_buttons(node: hou.Node) -> None:
    """Run by the site's event scripts when a File Cache is created or loaded."""
    if node.isInsideLockedHDA():
        return
    group = node.parmTemplateGroup()
    after = BUTTON_AFTER
    changed = False
    for index, (name, label, callback, help_text) in enumerate(BUTTONS):
        # A copied node brings its buttons along, and adding one twice raises.
        if group.find(name) is None:
            button = hou.ButtonParmTemplate(
                name,
                label,
                script_callback=callback,
                script_callback_language=hou.scriptLanguage.Python,
                help=help_text,
                join_with_next=index < len(BUTTONS) - 1,
            )
            group.insertAfter(after, button)
            changed = True
        after = name
    if changed:
        node.setParmTemplateGroup(group)


def sendable(node: hou.Node) -> bool:
    """A cache inside a locked asset can't be set to load from disk, so it is
    never offered as Cache first."""
    return node.type().name() == FILE_CACHE and not node.isInsideLockedHDA()


def upstream(node: hou.Node) -> list[hou.Node]:
    """The sendable File Caches `node` reads from, each after the caches it reads."""
    return [n for n in reached(node) if n != node and sendable(n)]


def reached(
    node: hou.Node, stop: Callable[[hou.Node], bool] = lambda _: False
) -> list[hou.Node]:
    """Every node `node` cooks from, each after the nodes it cooks from.

    A node for which `stop` is true is reached, but not walked past.
    """
    found: list[hou.Node] = []
    seen = {(node.sessionId(), True)}
    stack = [(node, iter(_feeds(node, True, node)))]
    while stack:
        current, feeds = stack[-1]
        nxt = next(feeds, None)
        if nxt is None:
            stack.pop()
            if current not in found:
                found.append(current)
            continue
        if (nxt[0].sessionId(), nxt[1]) not in seen:
            seen.add((nxt[0].sessionId(), nxt[1]))
            walked = [] if stop(nxt[0]) else _feeds(nxt[0], nxt[1], node)
            stack.append((nxt[0], iter(walked)))
    return found


def _feeds(node: hou.Node, whole: bool, sent: hou.Node) -> list[tuple[hou.Node, bool]]:
    """What `node` cooks from, each with whether to follow it whole."""
    inputs = [n for n in node.inputs() if n is not None]
    if not whole:
        return [(n, not _within(sent, n)) for n in inputs]
    found = inputs + list(node.references(include_children=False))
    # An asset cooks its output node, which needn't carry the display flag.
    found += node.subnetOutputs()
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
