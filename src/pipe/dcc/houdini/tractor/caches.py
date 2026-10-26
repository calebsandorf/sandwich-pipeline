"""Stock File Cache 2.0 nodes as the farm sees them: the buttons the site adds,
the caches upstream of one, and the frames each of its tasks writes."""

from __future__ import annotations

import hou

from pipe.dcc.houdini.tractor.cache_task import ROP, STEP_TOLERANCE
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
# Any name, so folders compare by everything but their name.
FOLDER = "folder"

LOAD_FROM_DISK = "loadfromdisk"
SIMULATION = "cachesim"
# The networks whose output is their display node. A VOP or ROP network has
# none, and the /stage network's is the whole stage.
DISPLAYED = (hou.ObjNode, hou.SopNode, hou.DopNode, hou.LopNode)
# Frames per task of a cache that is not a simulation. Each task loads the hip
# again, so a chunk is several frames rather than one.
CHUNK = 10


def add_buttons(node: hou.Node) -> None:
    """Run by the site's event scripts when a File Cache is created or loaded,
    and after a save."""
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


def strip_buttons(node: hou.Node) -> None:
    """Run before a save. Any spare parm makes the hip save the node's whole
    layout as spares, so the buttons stay out of the file and come back after."""
    if node.isInsideLockedHDA():
        return
    for name, *_ in BUTTONS:
        if parm := node.parmTuple(name):
            # The stubs, made for Houdini 19.5, leave this method out.
            node.removeSpareParmTuple(parm)  # ty: ignore[unresolved-attribute]
    # Removing the buttons leaves the layout's folders behind as spares, which
    # warn on the next load. An artist's own edit to the layout, even one with
    # no spare parm such as a hidden parm, is saved by those spares.
    if _layout(node.parmTemplateGroup().entries()) == _layout(
        node.type().parmTemplateGroup().entries()
    ):
        node.removeSpareParms()


def _layout(templates: tuple[hou.ParmTemplate, ...]) -> list[hou.ParmTemplate]:
    """The templates with folder names left out, since Houdini renames the
    folders each time a layout is set."""
    found = []
    for template in templates:
        if isinstance(template, hou.FolderParmTemplate):
            folder = template.clone()
            folder.setName(FOLDER)
            # The stubs type clone() as returning a plain ParmTemplate.
            folder.setParmTemplates(())  # ty: ignore[unresolved-attribute]
            found.append(folder)
            found.extend(_layout(template.parmTemplates()))
        else:
            found.append(template)
    return found


def on_save(event: hou.hipFileEventType) -> None:
    """The site's hip file event callback."""
    if event == hou.hipFileEventType.BeforeSave:
        change = strip_buttons
    elif event == hou.hipFileEventType.AfterSave:
        change = add_buttons
    else:
        return
    # nodeType is None for a type not installed; File Cache ships with Houdini.
    file_cache = hou.sopNodeTypeCategory().nodeType(FILE_CACHE)
    # Neither is the artist's edit, so neither goes on their undo stack.
    with hou.undos.disabler():
        for node in file_cache.instances():  # ty: ignore[unresolved-attribute]
            change(node)


def sendable(node: hou.Node) -> bool:
    """A cache inside a locked asset can't be set to load from disk, so it is
    never offered as Cache first."""
    return node.type().name() == FILE_CACHE and not node.isInsideLockedHDA()


def upstream(node: hou.Node) -> list[hou.Node]:
    """The sendable File Caches `node` reads from, each after the caches it reads."""
    return [n for n in reached(node) if n != node and sendable(n)]


def between(sources: list[hou.Node], target: hou.Node) -> list[hou.Node]:
    """The File Caches loading from disk that `target` reads any of `sources`
    through, each after the caches it reads. Each stands in for everything
    behind it, so a change at a source reaches `target` only once the cache is
    written again."""
    found, steps = _walk(target)
    wanted = {_id(source) for source in sources}
    reaches = {step for step in steps if step[0] in wanted}
    # The steps come after what they cook from, so one pass settles all but a
    # reference looping back to a step still being walked.
    changed = True
    while changed:
        changed = False
        for step, feeds in steps.items():
            if step not in reaches and any(feed in reaches for feed in feeds):
                reaches.add(step)
                changed = True
    reaching = {node for node, _ in reaches}
    return [
        cache
        for cache in found
        if cache != target
        and _id(cache) not in wanted
        and cache.type().name() == FILE_CACHE
        and cache.evalParm(LOAD_FROM_DISK)
        and _id(cache) in reaching
    ]


def reached(node: hou.Node) -> list[hou.Node]:
    """Every node `node` cooks from, each after the nodes it cooks from."""
    return _walk(node)[0]


# A node of a walk by its session id, and whether the walk enters it.
Step = tuple[int, bool]


def _walk(start: hou.Node) -> tuple[list[hou.Node], dict[Step, list[Step]]]:
    """Every node `start` cooks from, each after the nodes it cooks from, and
    every step of the walk, in the same order, with the steps it cooks from."""
    found: dict[int, hou.Node] = {}
    steps: dict[Step, list[Step]] = {}
    first: Step = (_id(start), True)
    seen = {first}
    stack = [(start, first, iter(_feeds(start, True, start)), [])]
    while stack:
        current, step, feeds, fed = stack[-1]
        nxt = next(feeds, None)
        if nxt is None:
            stack.pop()
            steps[step] = fed
            found.setdefault(step[0], current)
            continue
        node, enter = nxt
        key = (_id(node), enter)
        fed.append(key)
        if key not in seen:
            seen.add(key)
            stack.append((node, key, iter(_feeds(node, enter, start)), []))
    return list(found.values()), steps


def _feeds(node: hou.Node, enter: bool, start: hou.Node) -> list[tuple[hou.Node, bool]]:
    """What `node` cooks from, each with whether to enter it. A network holding
    the node it was reached from, or the walk's start, is downstream of its
    contents, so only its inputs are followed."""
    inputs = [n for n in node.inputs() if n is not None]
    if not enter:
        return [(n, not _within(start, n)) for n in inputs]
    found = inputs + list(node.references(include_children=False))
    # An asset cooks its output node, which needn't carry the display flag.
    found += node.subnetOutputs()
    display = node.displayNode() if isinstance(node, DISPLAYED) else None
    if display is not None:
        found.append(display)
    return [(n, not (_within(node, n) or _within(start, n))) for n in found]


def _id(node: hou.Node) -> int:
    # The stubs say sessionId() returns None. It returns the node's id.
    return node.sessionId()  # ty: ignore[invalid-return-type]


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
