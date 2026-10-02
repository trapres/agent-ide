#!/usr/bin/env python3
"""Concatenate strings supplied as command-line arguments."""

import sys


def concatenate(*strings):
    """Return the strings joined together without adding a separator."""
    return "".join(strings)


def main(argv):
    if not argv:
        print("usage: concatenate.py STRING [STRING ...]", file=sys.stderr)
        return 1

    print(concatenate(*argv))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
