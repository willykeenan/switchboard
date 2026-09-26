"""python3 -m switchboard --demo"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent
# Installed wheel: the board modules live in switchboard/app.
# Source checkout: they live next to the switchboard/ directory.
HERE = PACKAGE / "app" if (PACKAGE / "app" / "server.py").is_file() else PACKAGE.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))


def build_parser():
    parser = argparse.ArgumentParser(
        prog="python3 -m switchboard",
        description="Serve the Switchboard local coordination board.",
    )
    parser.add_argument("--demo", action="store_true", help="Seed invented agents, rooms and a sample workflow")
    parser.add_argument(
        "--allow-launch",
        action="store_true",
        help="Let the legacy automatic dispatcher start codex/claude processes (off by default; "
             "also needs `boardctl.py routing --mode auto` and agents registered with --wake-mode resume)",
    )
    parser.add_argument("--install", action="store_true", help="Generate the runtime source manifest and exit")
    parser.add_argument("--host", default=os.environ.get("SWITCHBOARD_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("SWITCHBOARD_PORT", "47834")))
    parser.add_argument(
        "--data-dir",
        default=os.environ.get("SWITCHBOARD_HOME") or os.environ.get("SWITCHBOARD_ROOT") or str(Path.home() / ".switchboard"),
        help="Writable data directory (default: ~/.switchboard)",
    )
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    data_dir = Path(args.data_dir).expanduser()
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.environ["SWITCHBOARD_ROOT"] = str(data_dir)
    os.environ["SWITCHBOARD_HOME"] = str(data_dir)
    os.environ["SWITCHBOARD_ROOMS_ROOT"] = str(data_dir / "rooms")
    os.environ["SWITCHBOARD_HOST"] = args.host
    os.environ["SWITCHBOARD_PORT"] = str(args.port)
    os.environ.setdefault("SWITCHBOARD_DISABLE_ROOM", "1")
    if args.allow_launch:
        os.environ["SWITCHBOARD_ALLOW_LAUNCH"] = "1"

    from runtime_source import snapshot

    if args.install:
        try:
            result = snapshot(HERE, generate=True)
        except OSError as error:
            print("Could not write runtime-source-manifest.json in", HERE, "-", error, file=sys.stderr)
            return 1
        print("Wrote runtime-source-manifest.json with", len(result["files"]), "files")
        return 0

    if args.demo:
        import demo

        seeded = demo.seed(data_dir, data_dir / "rooms")
        print("Demo data:", seeded)

    import server

    server.HOST = args.host
    server.PORT = args.port
    server.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
