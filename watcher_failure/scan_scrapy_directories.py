# scan_scrapy_directories.py
import os
import re
import datetime
import logging
from pathlib import Path
from typing import List, Union

logger = logging.getLogger(__name__)

DATE_FMT = "%Y-%m-%d"


import os
import re
import datetime
from pathlib import Path
from typing import List, Union

# make sure you have this somewhere
DATE_FMT = "%Y-%m-%d"

def scan_scrapy_directories(
    log_directory: str,
    days: int,
    user_name: Union[str, List[str]],
    suite_name: str,
    version: str,
    branch_name: str,
    flavor: str,
    verbose: bool = False,
    db_name: str = None,
    **kwargs,
) -> List[str]:
    """
    Return list of directories under `log_directory` matching the pattern:
      {user_name}-{YYYY-MM-DD_time}-{suite}{...}-{version}(-release)?-distro-{flavor}-smithi
    where `user_name` can be:
      - a single string,
      - a list of strings, or
      - '*' (or ['*']) to match any user.
    Only directories whose date is within the past `days` days are returned.
    """
    base = Path(log_directory)
    cutoff = datetime.date.today() - datetime.timedelta(days=days)

    # --- build the “user” part of the regex ---
    if isinstance(user_name, (list, tuple)):
        # ['*'] → wildcard, otherwise an alternation of literal names
        if "*" in user_name:
            users_pat = r"[^-]+"             # “one or more non‐dash chars”
        else:
            users_pat = "(?:" + "|".join(re.escape(u) for u in user_name) + ")"
    else:
        if user_name == "*":
            users_pat = r"[^-]+"
        else:
            users_pat = re.escape(user_name)

    # suite name verbatim
    suite_pat = re.escape(suite_name)
    # allow optional “-branch” bits after suite (you can tighten this up if you know branch_name)
    branch_pat = r"(?:[:\-][^-]+)*"

    # extract the date from the directory name
    date_re = r"(?P<date>\d{4}-\d{2}-\d{2})_[0-9]{2}:[0-9]{2}:[0-9]{2}"

    if version == "main":
        # we expect nothing before -distro- (only optional branch name before)
        pattern = (
            rf"^{users_pat}-"
            rf"{date_re}-"
            rf"{suite_pat}(?:-[^-]+)*"
            rf"(?=-distro-)"                   # Lookahead for -distro-
            rf"(?:-release)?-distro-"
            rf"{re.escape(flavor)}-(?:smithi|trial)$"
        )
    else:
        # we expect explicit -version before -distro-
        pattern = (
            rf"^{users_pat}-"
            rf"{date_re}-"
            rf"{suite_pat}(?:-[^-]+)*"
            rf"-{re.escape(version)}"
            rf"(?:-release)?-distro-"
            rf"{re.escape(flavor)}-(?:smithi|trial)$"
        )

    regex = re.compile(pattern)
    if verbose:
        logging.debug("Using directory regex: %s", pattern)

    results: List[str] = []
    try:
        for entry in os.scandir(base):
            if not entry.is_dir():
                logging.debug("Skipping %s: not a directory", entry.name)
                continue
            m = regex.match(entry.name)
            if not m:
                logging.debug("Skipping %s: does not match pattern %s", entry.name, pattern)
                if entry.name.startswith('skanta-2025-05-22'):
                    logging.debug("Skipping %s: does not match regex %s", entry.name, pattern)
                continue
            logging.debug("Directory %s matched regex", entry.name)
            if version == "main":
                name_before_distro = entry.name.split("-distro-")[0]
                contains_known_version = any(f"-{ver}" in name_before_distro for ver in ["reef", "tentacle", "quincy", "squid"])
                if contains_known_version:
                    if verbose and entry.name.startswith('skanta-2025'):
                            logging.debug("Skipping %s: contains known version (main mode)", entry.name)
                    continue

            # check date cutoff
            date_str = m.group("date")
            try:
                d = datetime.datetime.strptime(date_str, DATE_FMT).date()
            except ValueError:
                if verbose:
                    logging.debug("Skipping %s: bad date %r", entry.name, date_str)
                continue
            if d < cutoff:
                #if verbose:
                #    logging.debug("Skipping %s: %s older than %s", entry.name, d, cutoff)
                continue

            full = str(base / entry.name)
            if verbose:
                logging.debug("Accepting directory: %s", full)
            results.append(full)

    except FileNotFoundError:
        logging.error("Log directory not found: %s", base)
    except Exception:
        logging.exception("Error scanning directories")

    return results
