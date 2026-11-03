#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025 Jonah Brüchert <jbb@kaidan.im>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Convert http, url and transitland-atlas sources of region files to
mobility-database sources, where the same feed exists in the Mobility Database.

Matching is done primarily by comparing download URLs. The Transitland ID to
MDB ID mapping spreadsheet is only used as a fallback, and only if the
suggested MDB feed passes a plausibility check, because the mapping is known
to be wrong at times.

Converted sources keep downloading from the same url as before: if the MDB
url differs (even if only in http vs https), the previous url is kept as
url-override. Redirects of deprecated MDB feeds are not applied, use
mdb-apply-redirects.py for that.

Usage: mdbify.py [--dry-run] feeds/<region>.json...
"""

from mobilitydatabase import Database
from transitland import Atlas
from urllib.parse import urlparse
from pathlib import Path
from collections import defaultdict
from typing import Optional
from utils import eprint

import json
import sys
import csv
import re
import unicodedata
import requests


def canonicalize_url(url: str) -> str:
    """Normalize scheme, www. prefix and trailing slashes"""
    parsed = urlparse(url.strip())
    netloc = parsed.netloc.lower().removeprefix("www.")
    path = parsed.path.rstrip("/")
    return netloc + path + (f"?{parsed.query}" if parsed.query else "")


def tokens(text: str) -> set[str]:
    stopwords = {
        "transit", "transport", "transportes", "transports", "transporte",
        "transportation", "authority", "regional", "region", "metro",
        "public", "city", "county", "district", "service", "services",
        "system", "agency", "the", "and", "des", "del", "der", "die", "gtfs",
        "bus", "buses", "feed", "inc", "ltd", "llc", "gmbh", "company",
        "for", "von", "les", "los", "las", "rail", "train", "trains", "ferry",
        "www", "com", "org", "net", "gov", "zip", "rt",
    }
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return {t for t in re.split(r"[^a-z0-9]+", text) if len(t) >= 3 and t not in stopwords}


def unversioned_url(url: str) -> str:
    """Url without path segments that look like versions or timestamps, e.g. /v1683228856/"""
    parsed = urlparse(url.strip())
    segments = [s for s in parsed.path.lower().split("/") if s and not re.fullmatch(r"v?\d{6,}", s)]
    return parsed.netloc.lower().removeprefix("www.") + "/" + "/".join(segments)


STATUS_RANK = {"active": 0, "inactive": 1, "future": 2, "development": 3, "deprecated": 4}

# Transitland url kind -> (MDB data_type, MDB entity_type)
TL_URL_KINDS = {
    "static_current": ("gtfs", ""),
    "realtime_trip_updates": ("gtfs_rt", "tu"),
    "realtime_vehicle_positions": ("gtfs_rt", "vp"),
    "realtime_alerts": ("gtfs_rt", "sa"),
}


class Matcher:
    def __init__(self):
        self.mdb = Database.load()
        self.atlas = Atlas.load(Path("transitland-atlas"))

        # Operators are often defined at file level and reference their feeds
        self.operator_names: dict[str, list[str]] = defaultdict(list)
        self.atlas_by_url: dict[str, set[str]] = defaultdict(set)
        for f in Path("transitland-atlas/feeds").iterdir():
            dmfr = json.load(open(f, "r", encoding="utf-8"))
            for op in dmfr.get("operators", []):
                for assoc in op.get("associated_feeds", []):
                    if "feed_onestop_id" in assoc:
                        self.operator_names[assoc["feed_onestop_id"]] += \
                            [op.get("name", ""), op.get("short_name", ""), op.get("onestop_id", "")]
            for feed in dmfr["feeds"]:
                for kind, urls in feed.get("urls", {}).items():
                    for url in urls if isinstance(urls, list) else [urls]:
                        if isinstance(url, str):
                            self.atlas_by_url[canonicalize_url(url).lower()].add(feed["id"])
                for op in feed.get("operators", []):
                    self.operator_names[feed["id"]] += \
                        [op.get("name", ""), op.get("short_name", ""), op.get("onestop_id", "")]

        self.by_url: dict[str, list[str]] = defaultdict(list)
        self.by_url_lower: dict[str, list[str]] = defaultdict(list)
        for feed in self.mdb.by_id.values():
            if not feed["urls.direct_download"]:
                continue
            url = canonicalize_url(feed["urls.direct_download"])
            self.by_url[url].append(feed["id"])
            self.by_url_lower[url.lower()].append(feed["id"])

        self.mapping: dict[str, list[str]] = defaultdict(list)
        id_mapping_file = "mobilitydatabase-mapping.csv"
        if not Path(id_mapping_file).exists():
            table = requests.get("https://docs.google.com/spreadsheets/d/1Q96KDppKsn2khdrkraZCQ7T_qRSfwj7WsvqXvuMt4Bc/export?format=csv&gid=1787149399").text
            with open(id_mapping_file, "w") as f:
                f.write(table)
        with open(id_mapping_file, "r") as f:
            for row in csv.DictReader(f, delimiter=",", quotechar="\""):
                if row.get("Transitland ID", "").strip() and row["mdb_source_id"].strip():
                    self.mapping[row["Transitland ID"].strip()].append(f"mdb-{row['mdb_source_id'].strip()}")

    def follow_redirects(self, mdb_id: str) -> list[str]:
        """Resolve deprecated feeds to the feeds that replace them"""
        seen = set()
        todo = [mdb_id]
        result = []
        while todo:
            current = todo.pop(0)
            if current in seen or current not in self.mdb.by_id:
                continue
            seen.add(current)
            redirect = self.mdb.by_id[current].get("redirect.id")
            if self.mdb.by_id[current]["status"] == "deprecated" and redirect:
                todo += [r.strip() for r in redirect.split("|") if r.strip()]
            else:
                result.append(current)
        return result

    def best(self, ids: list[str]) -> Optional[str]:
        ids = list(dict.fromkeys(ids))
        if not ids:
            return None
        return min(ids, key=lambda i: STATUS_RANK.get(self.mdb.by_id[i]["status"], 5))

    def by_download_url(self, url: str, data_type: str, entity_type: str = "") -> list[str]:
        canonical = canonicalize_url(url)
        candidates = self.by_url.get(canonical) or self.by_url_lower.get(canonical.lower()) or []
        result = []
        for mdb_id in candidates:
            feed = self.mdb.by_id[mdb_id]
            if feed["data_type"] != data_type:
                continue
            if entity_type and feed["entity_type"] and entity_type not in feed["entity_type"].split("|"):
                continue
            result.append(mdb_id)
        return result

    def plausible(self, mdb_id: str, atlas_feed: dict, atlas_url: str, country: Optional[str]) -> bool:
        """
        Check whether an MDB feed suggested by the mapping plausibly is the same as the atlas feed.
        Only used if the download urls differ.
        """
        feed = self.mdb.by_id[mdb_id]
        mdb_url = feed["urls.direct_download"]

        # Switching to an outdated url would be a regression
        if feed["status"] != "active":
            return False

        # Same url, except for a version in the path
        if unversioned_url(mdb_url) == unversioned_url(atlas_url):
            return True

        # Without a country to compare to (e.g. eu.json), names alone are not enough
        if not country or feed["location.country_code"].upper() != country:
            return False

        # The MDB url belongs to a different atlas feed
        claimed_by = self.atlas_by_url.get(canonicalize_url(mdb_url).lower(), set())
        if claimed_by and atlas_feed["id"] not in claimed_by:
            return False

        mdb_tokens = tokens(feed["provider"]) | tokens(feed["name"])
        atlas_text = atlas_feed["id"] + " " + " ".join(self.operator_names.get(atlas_feed["id"], []))
        return bool(mdb_tokens & tokens(atlas_text))

    def match_transitland(self, source: dict, country: Optional[str]) -> tuple[Optional[list[tuple[str, str]]], str]:
        """
        Returns a list of (url kind, mdb id, atlas url) for all urls of the atlas feed,
        or None with a reason if the source can not be converted
        """
        tl_id = source["transitland-atlas-id"]
        atlas_feed = self.atlas.by_id.get(tl_id)
        if not atlas_feed:
            return None, "not in transitland atlas"
        if "api-key" in source:
            return None, "uses api-key, which mobility-database sources do not support"
        if "authorization" in atlas_feed and "url-override" not in source:
            # Without credentials such sources are ignored, after conversion they would be fetched and fail
            return None, f"requires authorization ({atlas_feed['authorization'].get('type')})"

        urls = {kind: url for kind, url in atlas_feed["urls"].items() if kind in TL_URL_KINDS}
        if "static_historic" in atlas_feed["urls"] and "static_current" not in atlas_feed["urls"]:
            urls["static_current"] = atlas_feed["urls"]["static_historic"][0]
        unsupported = set(atlas_feed["urls"]) - set(TL_URL_KINDS) - {"static_historic", "static_planned"}
        if unsupported:
            return None, f"has unsupported url types {sorted(unsupported)}"
        if not urls:
            return None, "has no urls"
        if len(urls) > 1 and "url-override" in source:
            return None, "url-override would apply to multiple feeds"
        if "http-options" in source and any(kind != "static_current" for kind in urls):
            return None, "http-options are not supported for realtime mobility-database sources"

        mapped = [i for m in self.mapping.get(tl_id, []) for i in self.follow_redirects(m)]

        result = []
        for kind, url in urls.items():
            data_type, entity_type = TL_URL_KINDS[kind]
            # No redirects here, applying them is up to mdb-apply-redirects.py
            by_url = self.by_download_url(url, data_type, entity_type)
            mapped_kind = [i for i in mapped if self.mdb.by_id[i]["data_type"] == data_type
                           and (not entity_type or entity_type in self.mdb.by_id[i]["entity_type"].split("|"))]

            if by_url:
                # prefer the url match that the mapping agrees with
                agreed = [i for i in by_url if i in mapped_kind]
                chosen = self.best(agreed or by_url)
                if mapped_kind and not agreed:
                    eprint(f"Info: {tl_id} ({kind}): mapping suggests {mapped_kind}, but url matches {chosen}")
            elif mapped_kind:
                plausible = [i for i in mapped_kind if self.plausible(i, atlas_feed, url, country)]
                if not plausible:
                    return None, f"mapping suggests {mapped_kind} for {kind}, but it does not look like the same feed"
                chosen = self.best(plausible)
                eprint(f"Info: {tl_id} ({kind}): no url match, using {chosen} from mapping")
            else:
                return None, f"no mobility database feed found for {kind}"
            if chosen not in (c for _, c, _ in result):
                result.append((kind, chosen, url))

        return result, ""

    def match_url(self, source: dict, data_type: str) -> Optional[str]:
        if source["type"] == "url" and "headers" in source:
            return None
        return self.best(self.by_download_url(source["url"], data_type))

    def preserve_url(self, source: dict, mdb_id: str, url: str):
        """Keep downloading from the same url as before, if the MDB url differs (even just in scheme)"""
        if "url-override" not in source and self.mdb.by_id[mdb_id]["urls.direct_download"].strip() != url.strip():
            source["url-override"] = url


def convert(matcher: Matcher, region: dict, region_file: str) -> int:
    converted = 0
    new_sources = []
    # Region files are named after ISO country codes, optionally with a subdivision (e.g. us-ca)
    prefix = Path(region_file).stem.split("-")[0]
    country = prefix.upper() if len(prefix) == 2 and prefix != "eu" else None

    for source in region["sources"]:
        match source["type"]:
            case "http":
                mdb_id = matcher.match_url(source, "gtfs")
                if mdb_id:
                    url = source["url"]
                    source = {"name": source["name"], "type": "mobility-database", "mdb-id": mdb_id} | \
                        {k: v for k, v in source.items() if k not in ("name", "type", "url")}
                    matcher.preserve_url(source, mdb_id, url)
                    converted += 1
            case "url" if source.get("spec") == "gtfs-rt":
                mdb_id = matcher.match_url(source, "gtfs_rt")
                if mdb_id:
                    url = source["url"]
                    source = {"name": source["name"], "type": "mobility-database", "mdb-id": mdb_id, "spec": "gtfs-rt"} | \
                        {k: v for k, v in source.items() if k not in ("name", "type", "url", "spec")}
                    matcher.preserve_url(source, mdb_id, url)
                    converted += 1
            case "transitland-atlas":
                matches, reason = matcher.match_transitland(source, country)
                if matches is None:
                    eprint(f"Info: {region_file}: Not converting {source['name']} ({source['transitland-atlas-id']}): {reason}")
                else:
                    rest = {k: v for k, v in source.items() if k not in ("name", "type", "transitland-atlas-id", "spec")}
                    for kind, mdb_id, url in matches:
                        new_source = {"name": source["name"], "type": "mobility-database", "mdb-id": mdb_id}
                        if kind != "static_current":
                            new_source["spec"] = "gtfs-rt"
                        new_source |= rest
                        matcher.preserve_url(new_source, mdb_id, url)
                        new_sources.append(new_source)
                    converted += 1
                    continue
        new_sources.append(source)

    region["sources"] = new_sources
    return converted


if __name__ == "__main__":
    args = sys.argv[1:]
    dry_run = "--dry-run" in args
    region_files = [a for a in args if not a.startswith("--")]

    matcher = Matcher()

    for region_file in region_files:
        region = json.load(open(region_file, "r"))
        converted = convert(matcher, region, region_file)
        print(f"{region_file}: converted {converted} source(s)")

        if not dry_run and converted:
            with open(region_file, "w") as region_out:
                region_out.write(json.dumps(region, indent=4, ensure_ascii=False))
                region_out.write("\n")
