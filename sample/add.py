#!/usr/bin/env python3
"""Add an arbitrary number of numbers together."""

import sys


def add(*numbers):
    """Return the sum of any number of numeric arguments."""
    total = 0
    for n in numbers:
        total += n
    return total


def main(argv):
    if not argv:
        print("usage: add.py NUMBER [NUMBER ...]", file=sys.stderr)
        return 1

    try:
        numbers = [float(a) for a in argv]
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    total = add(*numbers)
    # Print integers without a trailing ".0"
    print(int(total) if total.is_integer() else total)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
