import sys

# Absolute imports so this module also works when loaded outside "python -m".
from kopy.cli import parse_args
from kopy.engine import FatalError, run


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        code = run(args)
    except FatalError as e:
        print(f"kopy: fatal: {e}", file=sys.stderr)
        code = 16
    except KeyboardInterrupt:
        print("kopy: interrupted", file=sys.stderr)
        code = 16
    return code


if __name__ == "__main__":
    sys.exit(main())
