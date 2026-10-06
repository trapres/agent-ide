"""Pure geometry so resizing never depends on curses or a child process."""
from dataclasses import dataclass
import re


PANES = ("agent", "activity", "visualization")
PRESETS = ("default", "agent-right", "agent-top", "visualization-top")
MINIMUMS = {"agent": (12, 40), "activity": (8, 30), "visualization": (8, 30)}


@dataclass(frozen=True)
class Pane:
    pane: str


@dataclass(frozen=True)
class Split:
    id: str
    axis: str
    ratio_bps: int
    first: object
    second: object


def parse_tree(value):
    """Validate a decoded v1 tree into immutable nodes, with bounded depth."""
    panes, splits = set(), set()

    def visit(node, depth):
        if depth > 3 or not isinstance(node, dict):
            raise ValueError("layout nodes must be objects with depth at most three")
        if node.get("type") == "pane":
            name = node.get("pane")
            if set(node) != {"type", "pane"} or not isinstance(name, str) or name not in PANES or name in panes:
                raise ValueError("layout requires each known pane exactly once")
            panes.add(name)
            return Pane(name)
        if node.get("type") != "split" or set(node) != {"type", "id", "axis", "ratio_bps", "first", "second"}:
            raise ValueError("invalid layout split fields")
        name, axis, ratio = node["id"], node["axis"], node["ratio_bps"]
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,31}", name) or name in splits:
            raise ValueError("layout split IDs must be valid and unique")
        if not isinstance(axis, str) or axis not in ("rows", "columns"):
            raise ValueError("layout split axis must be rows or columns")
        if type(ratio) is not int or not 1000 <= ratio <= 9000:
            raise ValueError("layout ratio_bps must be an integer from 1000 to 9000")
        splits.add(name)
        return Split(name, axis, ratio, visit(node["first"], depth + 1), visit(node["second"], depth + 1))

    tree = visit(value, 1)
    if panes != set(PANES) or len(splits) != 2:
        raise ValueError("layout requires exactly three panes and two splits")
    return tree


def tree_dict(tree):
    if isinstance(tree, Pane):
        return {"type": "pane", "pane": tree.pane}
    return {"type": "split", "id": tree.id, "axis": tree.axis, "ratio_bps": tree.ratio_bps,
            "first": tree_dict(tree.first), "second": tree_dict(tree.second)}


def preset(name="default"):
    if name not in PRESETS:
        raise ValueError("unknown layout preset: " + str(name))
    activity, visualization, agent = Pane("activity"), Pane("visualization"), Pane("agent")
    if name == "visualization-top":
        activity, visualization = visualization, activity
    review = Split("review", "columns" if name == "agent-top" else "rows", 5000, activity, visualization)
    first, second = (review, agent) if name == "agent-right" else (agent, review)
    return Split("main", "rows" if name == "agent-top" else "columns", 5000, first, second)


def pane_order(tree):
    if isinstance(tree, Pane):
        return (tree.pane,)
    return pane_order(tree.first) + pane_order(tree.second)


def minimum_size(tree):
    """Return (rows, columns), including each leaf's own border."""
    if isinstance(tree, Pane):
        return MINIMUMS[tree.pane]
    ah, aw = minimum_size(tree.first)
    bh, bw = minimum_size(tree.second)
    return (ah + bh, max(aw, bw)) if tree.axis == "rows" else (max(ah, bh), aw + bw)


def compact_reason(tree, rows, columns):
    if rows < 3 or columns < 4:
        return "terminal too small"
    if rows < 28 or columns < 100:
        return "terminal below 100x28"
    h, w = minimum_size(tree)
    if rows - 2 < h or columns < w:
        return "layout minimum %dx%d" % (w, h + 2)
    return None


def arrange(tree, rows, columns, focus="agent", maximized=False):
    """Allocate a validated tree without changing its requested ratios."""
    if focus not in PANES:
        raise ValueError("unknown focused pane")
    if rows < 3 or columns < 4:
        return {}
    area = Rect(1, 0, rows - 2, columns)
    if maximized or compact_reason(tree, rows, columns):
        return {focus: area}
    result = {}

    def allocate(node, rect):
        if isinstance(node, Pane):
            result[node.pane] = rect
            return
        axis = 0 if node.axis == "rows" else 1
        extent = (rect.height, rect.width)[axis]
        requested = (extent * node.ratio_bps + 9999) // 10000
        first = max(minimum_size(node.first)[axis], min(requested, extent - minimum_size(node.second)[axis]))
        if axis == 0:
            a = Rect(rect.y, rect.x, first, rect.width)
            b = Rect(rect.y + first, rect.x, extent - first, rect.width)
        else:
            a = Rect(rect.y, rect.x, rect.height, first)
            b = Rect(rect.y, rect.x + first, rect.height, extent - first)
        allocate(node.first, a)
        allocate(node.second, b)

    allocate(tree, area)
    return result


def reflect(tree):
    """Reflect columns throughout the tree; preserve rows and pane identity."""
    if isinstance(tree, Pane):
        return tree
    a, b = reflect(tree.first), reflect(tree.second)
    return Split(tree.id, tree.axis, 10000 - tree.ratio_bps, b, a) if tree.axis == "columns" else Split(tree.id, tree.axis, tree.ratio_bps, a, b)


def adjust_share(tree, pane, axis, delta):
    """Adjust the pane's nearest matching ancestor; return (tree, found)."""
    if isinstance(tree, Pane):
        return tree, False
    in_first = pane in pane_order(tree.first)
    child = tree.first if in_first else tree.second
    if pane not in pane_order(child):
        return tree, False
    changed, found = adjust_share(child, pane, axis, delta)
    if found:
        return Split(tree.id, tree.axis, tree.ratio_bps,
                     changed if in_first else tree.first, tree.second if in_first else changed), True
    if tree.axis != axis:
        return tree, False
    ratio = min(9000, max(1000, tree.ratio_bps + (delta if in_first else -delta)))
    return Split(tree.id, tree.axis, ratio, tree.first, tree.second), True


@dataclass(frozen=True)
class Rect:
    y: int
    x: int
    height: int
    width: int

    @property
    def content_size(self):
        return max(1, self.height - 2), max(1, self.width - 2)


def layout(rows, columns, side="left", agent_fraction=.5, activity_fraction=.5,
           focus="agent", maximized=False):
    """Legacy side/ratio geometry retained for existing callers."""
    if side not in ("left", "right"):
        raise ValueError("agent side must be left or right")
    if rows < 3 or columns < 4:
        return {}
    height = max(1, rows - 2)
    if maximized or columns < 100 or rows < 28:
        return {focus: Rect(1, 0, height, max(1, columns))}
    agent_width = columns - int(columns * (1 - min(.7, max(.3, agent_fraction))))
    review_width = columns - agent_width
    top = height - int(height * (1 - min(.7, max(.3, activity_fraction))))
    agent_x, review_x = (0, agent_width) if side == "left" else (review_width, 0)
    return {
        "agent": Rect(1, agent_x, height, agent_width),
        "activity": Rect(1, review_x, top, review_width),
        "visualization": Rect(1 + top, review_x, height - top, review_width),
    }
