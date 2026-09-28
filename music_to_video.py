"""Backward-compatible entry point for projects using the former module name."""

from seonyuldam import bundled_tool, convert, main


if __name__ == "__main__":
    raise SystemExit(main())
