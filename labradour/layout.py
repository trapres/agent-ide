"""Pure geometry so resizing never depends on curses or a child process."""
from dataclasses import dataclass


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
    """Default preset only. Arbitrary split trees are a Phase 3 decision."""
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
