"""Command line: python -m eptools {gui,check,run,inspect} ...

With no arguments the desktop window opens."""

import argparse
import sys

from . import check, runner


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] == "gui":
        from . import gui  # imported here so the command line works without tkinter
        return gui.main(argv[1:])
    if argv[0] == "check":
        return check.main(argv[1:])

    ap = argparse.ArgumentParser(prog="eptools", description="EnergyPlus run checker and parameter-study runner.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("gui", help="open the desktop window (the default when no command is given)")
    sub.add_parser("check", help="explain the warnings in one or more runs (see 'eptools check -h')")

    r = sub.add_parser("run", help="run a parameter study described in a JSON file")
    r.add_argument("study", help="study JSON file, e.g. examples/study.json")
    r.add_argument("--dry-run", action="store_true", help="write the edited IDFs and stop before running EnergyPlus")
    r.add_argument("--jobs", type=int, help="simulations to run at once (overrides the study file)")
    r.add_argument("--resume", action="store_true", help="skip runs that already finished with the same input")
    r.add_argument("--limit", type=int, help="only run the first N combinations (for testing)")
    r.add_argument("--timeout", type=int, default=3600, help="seconds before a single run is stopped")

    i = sub.add_parser("inspect", help="list the objects and fields in a model, to help write a study file")
    i.add_argument("model")
    i.add_argument("object", nargs="?", help="IDF class, e.g. Material. Leave out to list all classes")
    i.add_argument("--name", default="*", help="object name pattern, e.g. 'EPS*'")

    args = ap.parse_args(argv)
    try:
        if args.cmd == "run":
            return runner.run_study(args.study, args.dry_run, args.jobs, args.resume, args.timeout, args.limit)
        runner.inspect(args.model, args.object, args.name)
        return 0
    except runner.StudyError as exc:
        print(f"\nStopped: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
