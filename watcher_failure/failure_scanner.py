import logging
import re
import json
import sqlite3
import datetime

from pathlib import Path
from typing import List
from .reason_conversion import reason_conversion

from .scan_scrapy_directories import scan_scrapy_directories
from .config import Config
from typing import Tuple, Dict, List

logger = logging.getLogger(__name__)

# Conversion utilities

def convert_reason(long_reason: str) -> str:
    """
    Map a raw failure message to a concise reason using predefined mappings.
    """
    return reason_conversion.get(long_reason, long_reason)


def normalize_machine_name(log_string: str) -> str:
    """
    Normalize machine-specific details in a log string for consistent grouping.
    """
    s = re.sub(r'smithi\d+', 'smithi000', log_string)
    s = re.sub(r'trial\d+', 'smithi000', s)
    s = re.sub(r'CEPH_REF=[a-f0-9]+', 'CEPH_REF=XXXXXXXXXXXXXXXXXX', s)
    return s



class FailureRecord:
    __slots__ = ('directory', 'date', 'reason', 'job_id', 'version', 'flavor')

    def __init__(self, directory: str, date: str, reason: str, job_id: str, version: str = '', flavor: str = '') -> None:
        self.directory = directory
        self.date = date
        self.reason = reason
        self.job_id = job_id
        self.version = version
        self.flavor = flavor

    @classmethod
    def from_dict(cls, d):
        return cls(
            timestamp=datetime.fromisoformat(d["timestamp"]),
            reason=d["reason"],
            suite=d["suite"],
            node=d["node"],
            version=d["version"],
            flavor=d["flavor"],
            # …etc
        )
    
    # printable representation
    def __repr__(self) -> str:
        return f"FailureRecord(directory={self.directory}, date={self.date}, reason={self.reason}, job_id={self.job_id}, version={self.version}, flavor={self.flavor})"

class LogParser:
    """
    Parses scrape.log files in a directory to extract failure records.
    """
    FAILURE_RE = re.compile(r'^(?:Failure:|Timeout:?)\s*(.*)', re.MULTILINE)
    DEAD_RE    = re.compile(r'Dead: (.+)')
    JOB_RE     = re.compile(r'(\d+)')
    DATE_RE    = re.compile(r'(\d{4}-\d{2}-\d{2})_')

    def __init__(self, verbose: bool = False) -> None:
        level = logging.DEBUG if verbose else logging.INFO
        logging.basicConfig(level=level)
        self.verbose = verbose

    def parse_file(self, file_path: Path) -> List[FailureRecord]:
        failures: List[FailureRecord] = []
        current_reason: Optional[str] = None
        lines = file_path.read_text().splitlines()
        logger.debug(f"\n\n\nParsing file: {file_path}\n")
        for line in lines:
            logger.debug(f"Processing line: {line.strip()}\n")
            # High-importance backtrace marker
            if "MAX_BACKTRACE_LINES" in line:
                current_reason = "BACKTRACE"
                failures.append(FailureRecord(
                    directory=str(file_path.parent),
                    date=self._extract_date(file_path),
                    reason=current_reason,
                    job_id="unknown"
                ))
                continue

            m = self.FAILURE_RE.search(line)
            if m:
                logger.debug(f"Matched FAILURE_RE: {line.strip()}\n")
                # add back timeout keyword since it was stripped
                if line.startswith("Timeout"):
                    normalized = normalize_machine_name("Timeout " + m.group(1))
                else:
                    normalized = normalize_machine_name(m.group(1))
                logger.debug(f"Normalized failure reason: {normalized}\n")
                current_reason = convert_reason(normalized)
                if self.verbose:
                    logger.debug(f"Detected failure reason: {current_reason}")
                continue

            m = self.DEAD_RE.search(line)
            if m:
                normalized = normalize_machine_name(m.group(1))
                current_reason = convert_reason(normalized)
                if self.verbose:
                    logger.debug(f"Detected dead reason: {current_reason}")
                continue

            # Extract job IDs
            if current_reason:
                logger.debug(f"Current reason set to: {current_reason} for line: {line.strip()}\n")
                if "jobs:" in line:
                    for job in self.JOB_RE.findall(line):
                        logger.debug(f"Found job ID: {job} for reason: {current_reason}")
                        failures.append(FailureRecord(
                            directory=str(file_path.parent),
                            date=self._extract_date(file_path),
                            reason=current_reason,
                            job_id=job,
                        ))
                    current_reason = None  # Reset after processing job IDs
                else:
                    logger.debug(f"No job IDs found in line: {line.strip()}\n")
            else:
                logger.debug(f"No current reason set for line: {line.strip()}\n")

            # Handle "NN jobs" summary lines
            #m2 = re.search(r'(\d+) jobs', line)
            #if m2 and current_reason:
            #    logger.debug(f"Detected summary line with {m2.group(1)} jobs, reason: {current_reason} number of groups: {len(m2.groups())} group 0: {m2.group(0)} group 1: {m2.group(1)}")
            #    count = int(m2.group(1))
            #    for _ in range(count):
            #        failures.append(FailureRecord(
            #            directory=str(file_path.parent),
            #            date=self._extract_date(file_path),
            #            reason=current_reason,
            #            job_id="unknown"
            #        ))
        return failures

    def _extract_date(self, file_path: Path) -> str:
        m = self.DATE_RE.search(str(file_path))
        return m.group(1) if m else datetime.date.today().isoformat()


class FailureScanner:
    """
    Scans logs under a base directory, either a single scrape.log dir or all version/flavor dirs.
    """
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.base = Path(cfg.log_directory)

    def scan_directory(self, path: Path) -> Tuple[List[FailureRecord], List[str]]:
        """Scan a single directory containing scrape.log and return parsed records."""
        logger.debug("Scanning single directory: %s", path)
        log_file = path / 'scrape.log'
        if not log_file.exists():
            logger.error("No scrape.log in %s", path)
            return [], [str(path)]
        parser = LogParser(verbose=self.cfg.verbose)
        return parser.parse_file(log_file), [str(path)]

    def scan_tree(self) -> Tuple[Dict[str, Dict[str, List[FailureRecord]]], Dict[str, Dict[str, List[str]]]]:
        """
        Scan all version/flavor directories under base, group failures by
        version & flavor, and return a JSON blob.
        """
        logger.debug("Scanning full tree under: %s", self.base)
        parser = LogParser(verbose=self.cfg.verbose)

        # Structure: { version: { flavor: [ record_dict, ... ] } }
        result: Dict[str, Dict[str, List[Dict]]] = {}
        records: Dict[str, Dict[str, List[FailureRecord]]] = {
            version: {flavor: [] for flavor in self.cfg.flavors}
            for version in self.cfg.versions
        }
        grouped_dirs: Dict[str, Dict[str, List[str]]] = {
            version: {flavor: [] for flavor in self.cfg.flavors}
            for version in self.cfg.versions
        }
        logger.debug("Grouped directories initialized: %s", grouped_dirs)

        for version in self.cfg.versions:
            
            for flavor in self.cfg.flavors:
                suite_name = (
                    f"crimson-{self.cfg.suite_name}"
                    if flavor == 'crimson'
                    else self.cfg.suite_name
                )
                bot_users = (
                    "*" if flavor == 'crimson' 
                    else self.cfg.bot_users
                )
                logger.debug("Tree scan for version=%s flavor=%s suite_name=%s users=%s", version, flavor, suite_name, bot_users)
                dirs = scan_scrapy_directories(
                    log_directory=str(self.base),
                    days=self.cfg.days,
                    # drop db_name here if it's not needed for scanning
                    user_name=bot_users,
                    suite_name=suite_name,
                    version=version,
                    branch_name=self.cfg.branch_name,
                    flavor=flavor,
                    verbose=self.cfg.verbose,
                )
                grouped_dirs[version][flavor] = dirs
                logger.debug("Found %s directories for version=%s flavor=%s", dirs, version, flavor)

                for d in dirs:
                    log_path = Path(d) / 'scrape.log'
                    if log_path.exists():
                        recs = parser.parse_file(log_path)
                        records[version][flavor].extend(recs)
                        for r in recs:
                            r.version = version
                            r.flavor = flavor
                        logger.debug("Parsed %d records from %s", len(recs), log_path)
                    else:
                        logger.warning("No scrape.log found in %s", log_path)

        logger.debug("Total versions scanned: %d", len(records))
        return records, grouped_dirs