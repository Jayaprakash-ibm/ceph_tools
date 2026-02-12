#!/usr/bin/env python3
"""
trackers.py – helper for mapping teuthology/ceph test failure strings ⇢ Ceph Redmine

Key features
------------
* Read creds from ``~/.redmine``
* Aggressively **normalise** noisy failure strings (timestamps, daemon ids, long
  numbers, log‑level tags …) so the search query is generic but still
  meaningful.
* Hit the Redmine `/search.json` API once, then pick the **closest** match based
  on ``difflib.SequenceMatcher``.
* Tiny JSON cache so we don’t hammer Redmine when repeatedly processing the same
  logs.
"""
from __future__ import annotations

import configparser
import json
import logging
import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from redminelib import Redmine

logger = logging.getLogger(__name__)


class RedmineConnector:
    """Convenience wrapper around *python‑redmine* with caching and fuzzy search."""

    # ---------------------------------------------------------------------
    # life‑cycle helpers                                                   |
    # ---------------------------------------------------------------------
    def __init__(
        self,
        config_path: str | Path = "~/.redmine",
        cache_file: str | Path = "tracker_cache.json",
    ) -> None:
        logger.debug("Loading Redmine config from: %s", config_path)
        self.config = self._load_config(config_path)
        self.cache_path = (
            Path(cache_file).expanduser()
            if not Path(cache_file).is_absolute()
            else Path(cache_file)
        )
        logger.debug("Using cache file: %s", self.cache_path)
        self.cache: Dict[str, Any] = self._load_cache()

        red_cfg = self.config["redmine"] if self.config.has_section("redmine") else {}
        logger.debug("   Redmine config: %s", red_cfg)

        self.redmine = Redmine(
            red_cfg.get("url", "https://tracker.ceph.com"),
            username=red_cfg.get("username", ""),
            key=red_cfg.get("password", ""),
        )

        project_name = red_cfg.get("project_name", "Ceph")
        try:
            self.project_id = self.redmine.project.get(project_name).id  # type: ignore[attr-defined]
            logger.debug("Connected to Redmine project ID: %s", self.project_id)
        except Exception as exc:  # pragma: no cover – network issue
            logger.warning("Could not fetch Redmine project '%s': %s", project_name, exc)
            self.project_id = None

    # ---------------------------------------------------------------------
    # public entry‑point                                                   |
    # ---------------------------------------------------------------------
    def search_and_refine(
        self,
        search_string: str,
        *,
        status: Optional[str] = None,
        limit: int = 50,
    ) -> Dict[str, Any]:
        """Return ``{"issue_id": int, "link": str}`` or ``{}`` if nothing found."""
        if not isinstance(search_string, str):
            raise TypeError("search_string must be str")

        # 0. cheap cache look‑up ------------------------------------------------
        if search_string in self.cache:
            logger.debug("Cache hit for '%s'", search_string)
            return self.cache[search_string]

        # 1. normalise ----------------------------------------------------------
        q_norm = self._normalize_for_search(search_string)

        # 2. fetch possible issues ---------------------------------------------
        issues = self._fetch_issues(q_norm, status=status, limit=limit)
        if not issues:
            logger.debug("No issues found for '%s'", search_string)
            self.cache[search_string] = {}
            self._save_cache()
            return {}

        # 3. select the best match ---------------------------------------------
        best = self._find_best_match(q_norm, issues)
        if best is None:
            logger.debug("Could not identify a close enough Redmine issue for '%s'", search_string)
            self.cache[search_string] = {}
            self._save_cache()
            return {}

        issue_id, score, link = best
        logger.debug("Selected issue %s (score %.02f)", issue_id, score)

        result = {"issue_id": issue_id, "link": link}
        self.cache[search_string] = result
        self._save_cache()
        return result

    # ------------------------------------------------------------------
    # internal helpers                                                   |
    # ------------------------------------------------------------------
    @staticmethod
    def _load_config(path: str | Path) -> configparser.ConfigParser:
        cfg = configparser.ConfigParser()
        logging.debug("Reading config file: %s contents:\n%s", path, Path("/home/nmordch/.redmine").read_text())
        cfg.read(Path(path).expanduser())
        return cfg

    # cache -------------------------------------------------------------
    def _load_cache(self) -> Dict[str, Any]:
        try:
            with open(self.cache_path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save_cache(self) -> None:
        try:
            with open(self.cache_path, "w", encoding="utf-8") as fh:
                json.dump(self.cache, fh, indent=2)
            logger.debug("Cache saved → %s", self.cache_path)
        except Exception as exc:  # pragma: no cover – filesystem perms
            logger.warning("Could not persist cache: %s", exc)

    # Redmine search ----------------------------------------------------
    def _fetch_issues(self, query: str, *, status: Optional[str], limit: int) -> List[Any]:
        params: Dict[str, Any] = {
            "limit": limit,
            "all_words": True,
            "wiki_pages": False,
            "attachments": False,
            "open_issues": False,
            "query": query,
        }
        if status:
            params["status_id"] = status
        logger.debug("Searching Redmine with params: %s", params)
        return self.redmine.issue.search(**params)  # type: ignore[arg-type]

    # similarity scoring ------------------------------------------------
    def _find_best_match(self, original: str, issues: List[Any]) -> Optional[Tuple[int, float, str]]:
        scored: List[Tuple[int, float, str]] = []
        for it in issues:
            title = self._trim_after_colon(it.title)
            desc = it.description or ""
            score = max(
                SequenceMatcher(None, original, title).ratio(),
                SequenceMatcher(None, original, desc).ratio(),
            )
            scored.append((it.id, score, f"{self.config['redmine']['url']}/issues/{it.id}"))
        logger.debug("Scores for query '%s': %s", original, scored)
        return max(scored, key=lambda t: t[1], default=None)

    # text utilities ----------------------------------------------------
    def _normalize_for_search(self, reason: str) -> str:
        """Best‑effort clean‑up so we hit the important *words* only."""
        # strip surrounding quotes
        reason = reason.strip().lstrip("'\"").rstrip("'\"")

        # 1) remove leading ISO timestamp
        reason = re.sub(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.+-]+\s+", "", reason)

        # 2) remove daemon / host identifiers (osd.12, mon.foo123, mds.a, mgr.x)
        # also remove any (){}[] around them
        reason = re.sub(r"\b(?:osd|mon|mgr|mds)\.[A-Za-z0-9_-]+\b", "", reason)
        reason = re.sub(r"[(){}]", "", reason)
        logger.debug("Reason after removing daemon IDs: %s", reason)

        # 3) remove bracketed log level tags like [WRN] [ERR] …
        #reason = re.sub(r"\[[A-Z]{3}\]", "", reason)

        # 4) scrub *all* standalone numbers (int or float) – usually not helpful
        #reason = re.sub(r"\b\d+(?:\.\d+)?\b", "", reason)

        # 5) zap stray punctuation that only creates tokens (parentheses, colons)
        reason = re.sub(r"[()@:;\\\[\\]]", " ", reason)

        # 6) collapse whitespace
        reason = re.sub(r"\s+", " ", reason).strip()

        logger.debug("Normalized reason: %s", reason)
        return reason

    @staticmethod
    def _trim_after_colon(text: str) -> str:
        return text.split(":", 1)[1].strip() if ":" in text else text


# quick manual check ----------------------------------------------------
if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(message)s")
    conn = RedmineConnector()
    sample = (
        "2025-05-18T23:33:40.185645+0000 osd.6 (osd.6) 3 : cluster [WRN] OSD bench "
        "result of 999.270273 IOPS is not within the threshold limit range of "
        "1000.000000 IOPS and 80000.000000 IOPS for osd.6. IOPS capacity is "
        "unchanged at 21500.000000 IOPS. The recommendation is to establish the "
        "osd's IOPS capacity using other benchmark tools (e.g. Fio) and then "
        "override osd_mclock_max_capacity_iops_[hdd|ssd]."
    )
    print(conn.search_and_refine(sample))
