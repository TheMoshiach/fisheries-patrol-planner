"""Command line: ``python -m patrol_planner <command>``.

    sample    generate the synthetic sample and run the pipeline on it
    fetch     download real data from Global Fishing Watch, then run the pipeline
    build     re-run the pipeline on raw data that is already on disk
    report    print the accuracy results
    status    show which dataset is active and what it contains
"""

from __future__ import annotations

import argparse
import sys

from . import pipeline, reporting
from .config import load_settings
from .storage import data_mode, open_store, settings_for


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="patrol_planner", description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("sample", help="generate synthetic sample data and build it")
    fetch = commands.add_parser("fetch", help="download real data and build it")
    fetch.add_argument("--refresh", action="store_true", help="ignore cached API responses")
    build = commands.add_parser("build", help="re-run the pipeline on existing raw data")
    build.add_argument("--data", choices=["real", "sample"], default=None)
    report = commands.add_parser("report", help="print accuracy results")
    report.add_argument("--data", choices=["real", "sample"], default=None)
    commands.add_parser("status", help="show the active dataset")
    args = parser.parse_args(argv)

    settings = load_settings()

    if args.command == "sample":
        from .sources import sample

        store = open_store("sample")
        manifest = sample.generate_sample(store, settings)
        summary = pipeline.build(store, settings)
        print(manifest["label"])
        _print_summary(summary)
        return 0

    if args.command == "fetch":
        from . import jobs
        from .sources import ingest

        store = open_store("real")
        jobs.write_status("running", "Downloading from Global Fishing Watch")
        try:
            ingest.fetch_real(store, settings, refresh=args.refresh)
            print("Building rankings")
            _print_summary(pipeline.build(store, settings))
        except Exception as error:
            jobs.write_status("failed", str(error))
            print(f"Refresh failed: {error}")
            return 1
        jobs.write_status("done", f"Data now covers up to {settings.window.end_date}")
        print("Done.")
        return 0

    if args.command == "build":
        store = open_store(args.data)
        _print_summary(pipeline.build(store, settings_for(store)))
        return 0

    if args.command == "report":
        store = open_store(args.data)
        print(reporting.accuracy_report(store))
        return 0

    if args.command == "status":
        mode = data_mode()
        store = open_store(mode)
        manifest = store.read_manifest()
        print(f"Active dataset: {mode} ({store.root})")
        for key, value in manifest.items():
            print(f"  {key}: {value}")
        return 0

    return 1


def _print_summary(summary: dict) -> None:
    for key, value in summary.items():
        print(f"  {key.replace('_', ' ')}: {value}")


if __name__ == "__main__":
    sys.exit(main())
