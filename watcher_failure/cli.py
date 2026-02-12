import argparse
import logging
from .config import Config
from .runner import Runner


def main():
    parser = argparse.ArgumentParser(
        description="Watcher Failure CLI: scan logs, generate reports, and send email"
    )
    parser.add_argument(
        "--db_name", default="failures.db",
        help="SQLite database name (e.g., failures.db)"
    )
    parser.add_argument(
        "--email", nargs="*",
        help="Email address(es) to send reports to (space-separated)"
    )
    parser.add_argument(
        "--log_directory", required=True,
        help="Base log directory for bot mode or single scrape.log directory"
    )
    parser.add_argument(
        "--days", type=int, default=7,
        help="Number of days to scan back (default: 7)"
    )
    parser.add_argument(
        "--user_name", default="teuthology",
        help="Username prefix in teuthology directory names"
    )
    parser.add_argument(
        "--suite_name", default="rados",
        help="Suite name in directory pattern"
    )
    parser.add_argument(
        "--branch_name", default="main",
        help="Branch name to scan (e.g., main, squid, reef)"
    )
    parser.add_argument(
        "--flavor", default="default",
        help="Flavor to scan (default or crimson)"
    )
    parser.add_argument(
        "--error_message", default=None,
        help="Only include failures matching this error message"
    )
    parser.add_argument(
        "--keep_db", action="store_true",
        help="Retain database file after run"
    )
    parser.add_argument(
        "--bot", action="store_true",
        help="Enable bot mode: scan all versions/flavors under log_directory"
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Enable verbose debug logging"
    )

    args = parser.parse_args()

    # initialize logging
    logging.basicConfig(force=True,
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)5s %(name)s: %(message)s"
    )
    print("Logging initialized with level: %s" % logging.getLevelName(logging.getLogger().getEffectiveLevel()))
    print("verbose: %s" % args.verbose)
    logging.debug("CLI arguments: %s", args)

    # build config and run
    cfg = Config.from_args(args)
    Runner(cfg).run()


if __name__ == '__main__':
    main()