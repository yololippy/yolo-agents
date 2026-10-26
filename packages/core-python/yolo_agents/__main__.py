"""Run the generic Yolo Agents host in demo or PydanticAI mode."""

import sys

from .host import create_parser, run_host, validate_host_arguments


def main() -> int:
    parser = create_parser(__doc__)
    args = parser.parse_args()
    validate_host_arguments(args, parser)
    try:
        return run_host(args)
    except RuntimeError as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    sys.exit(main())
