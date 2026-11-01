#!/usr/bin/env python3
"""Offline regional POI preparation. No website crawling or application imports."""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tarfile
import time
import unicodedata
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
MAX_PAGE = 65536
# Food-serving venues and food retail only. Keep native selectors and the final
# eligibility check derived from the same allowlists; reference-only dependencies
# retained by osmium must never become unrelated searchable POIs.
SELECTION_PROFILE = "food-v1"
AMENITIES = (
    "restaurant", "fast_food", "cafe", "bar", "pub", "food_court", "ice_cream",
    "marketplace",
)
FOOD_SHOPS = (
    "bakery", "supermarket", "convenience", "grocery", "greengrocer", "deli",
    "butcher", "seafood", "fishmonger", "cheese", "dairy", "farm", "frozen_food",
    "health_food", "pasta", "pastry", "confectionery", "chocolate", "coffee",
    "tea", "spices", "nuts", "food",
)
SELECTORS = [
    "nwr/amenity=" + ",".join(AMENITIES),
    "nwr/shop=" + ",".join(FOOD_SHOPS),
]
KINDS = ("amenity", "shop", "tourism", "leisure", "railway", "aeroway")


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_bytes(encoded(value) + b"\n")
    temp.replace(path)


def load(path):
    return json.loads(Path(path).read_text())


def digest_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def plan(benchmark, regions):
    config = load(ROOT / "regions.json")
    jobs = []
    if benchmark == "full-regions":
        selected = config["national_regions"] if regions == "all" else regions.split(",")
        if not selected or len(selected) != len(set(selected)):
            raise ValueError("Choose distinct configured regions")
        targets = [(region.strip(), "full", None) for region in selected]
    else:
        selected = list(config["benchmarks"]) if benchmark == "all-benchmarks" else [benchmark]
        targets = []
        for name in selected:
            spec = config["benchmarks"][name]
            targets.extend((region, name, spec["bbox"]) for region in spec["regions"])
    for region, scope, bbox in targets:
        if region not in config["regions"] or not re.fullmatch(r"[a-z]+(?:-[a-z]+)*", region):
            raise ValueError("Unknown region: " + region)
        jobs.append({"region": region, "scope": scope, "key": region + "-" + scope,
                     "bbox": ",".join(map(str, bbox)) if bbox else ""})
    if not jobs or len(jobs) > 100:
        raise ValueError("Invalid matrix size")
    return {"include": jobs}


class SourceRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def check_url(url):
    from urllib.parse import urlsplit
    parts = urlsplit(url)
    if (parts.scheme != "https" or parts.hostname != "download.geofabrik.de"
            or parts.username or parts.password or parts.port not in (None, 443)):
        raise ValueError("Only HTTPS Geofabrik source URLs are allowed")


def curl_download(url, temporary, max_bytes, deadline, max_redirects=5):
    """Follow bounded redirects manually, validating each URL before connecting.

    Curl's --proto-redir restricts schemes, not hosts. Automatic --location
    would bypass our host policy; disallowing all redirects breaks Geofabrik's
    normal latest-to-dated-snapshot response instead.
    """
    from urllib.parse import urljoin
    elapsed = 0.0
    for redirects in range(max_redirects + 1):
        check_url(url)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Total download budget exceeded")
        result = subprocess.run([
            "curl", "--disable", "--fail",  # Ignore curlrc; do NOT auto-follow.
            "--proto", "=https", "--proto-redir", "=https",
            "--connect-timeout", "30", "--max-time", str(max(1, int(remaining))),
            "--retry", "0", "--max-filesize", str(max_bytes),
            "--user-agent", "RegionalPlaceIndex/0.1 (offline OSM index builder)",
            "--output", str(temporary), "--write-out", "%{json}", url,
        ], stdout=subprocess.PIPE, text=True, check=True, timeout=remaining + 5)
        transfer = json.loads(result.stdout)
        elapsed += float(transfer.get("time_total", 0))
        status = int(transfer["http_code"])
        if status in (301, 302, 303, 307, 308):
            location = transfer.get("redirect_url")
            if not isinstance(location, str) or not location:
                raise ValueError("Source redirect has no Location target")
            next_url = urljoin(url, location)
            check_url(next_url)  # Validate BEFORE issuing another request.
            if redirects == max_redirects:
                raise ValueError("Source redirect limit exceeded")
            temporary.unlink(missing_ok=True)
            url = next_url
            print(f"Download: following validated Geofabrik redirect {redirects + 1}/{max_redirects}", flush=True)
            continue
        if status != 200:
            raise ValueError(f"Unexpected download HTTP status: {status}")
        transfer.update(time_total=elapsed, resolved_url=url, validated_redirects=redirects)
        return transfer
    raise AssertionError("Unreachable redirect state")


def download(region, work, max_bytes=3_000_000_000, seconds=2100):
    url = load(ROOT / "regions.json")["regions"][region]
    check_url(url)
    work.mkdir(parents=True, exist_ok=True)
    opener = urllib.request.build_opener(SourceRedirect())
    headers = {"User-Agent": "RegionalPlaceIndex/0.1 (offline OSM index builder)"}
    deadline = time.monotonic() + seconds
    target = work / "source.osm.pbf"
    temporary = work / "source.partial"
    # Two attempts share ONE total deadline, rather than multiplying a 35-minute timeout.
    for attempt in range(2):
        print(f"Download {region}: starting attempt {attempt + 1}/2", flush=True)
        try:
            with opener.open(urllib.request.Request(url + ".md5", headers=headers), timeout=30) as reply:
                sidecar = reply.read(4097)
            match = re.match(rb"^([a-fA-F0-9]{32})(?:\s|$)", sidecar)
            if len(sidecar) > 4096 or not match:
                raise ValueError("Invalid checksum sidecar")
            remaining = deadline - time.monotonic()
            if remaining <= 0 or shutil.disk_usage(work).free < 2_000_000_000:
                raise TimeoutError("Download time/disk reserve exhausted")
            transfer = curl_download(url, temporary, max_bytes, deadline)
            md5 = hashlib.md5(usedforsecurity=False)
            sha256 = hashlib.sha256()
            size = 0
            with temporary.open("rb") as downloaded:
                for chunk in iter(lambda: downloaded.read(1024 * 1024), b""):
                    size += len(chunk)
                    if size > max_bytes or time.monotonic() >= deadline:
                        raise ValueError("Download size/time limit exceeded")
                    md5.update(chunk)
                    sha256.update(chunk)
            if shutil.disk_usage(work).free < 2_000_000_000:
                raise ValueError("Download disk reserve exceeded")
            if not size or md5.hexdigest() != match[1].decode().lower():
                raise ValueError("Source checksum mismatch; latest may have changed")
            temporary.replace(target)
            save(work / "source.json", {"region": region, "url": url, "bytes": size,
                 "sha256": sha256.hexdigest(), "md5": md5.hexdigest(), "checksum_verified": True,
                 "download_transport": "curl", "transfer_seconds": transfer.get("time_total"),
                 "transfer_bytes_per_second": transfer.get("speed_download"),
                 "resolved_url": transfer["resolved_url"], "validated_redirects": transfer["validated_redirects"]})
            return
        except Exception:
            temporary.unlink(missing_ok=True)
            if attempt or time.monotonic() >= deadline - 30:
                raise
            time.sleep(2)


def normalize(text):
    return " ".join("".join(c for c in unicodedata.normalize("NFKD", text.casefold())
                            if not unicodedata.combining(c)).split())


def aliases(tags):
    output = []
    for key in ("name", "name:en", "alt_name", "short_name", "official_name", "brand"):
        for raw in tags.get(key, "").split(";")[:4]:
            words = normalize(raw).split()
            for start in range(min(4, len(words))):
                term = " ".join(words[start:])[:160]
                if term and term not in output:
                    output.append(term)
    return sorted(output[:16])


def inactive(tags, today):
    truthy = {"yes", "true", "1", "closed", "permanently_closed", "permanently closed"}
    for key in ("closed", "disused", "abandoned", "demolished", "razed", "permanently_closed"):
        if tags.get(key, "").strip().lower() in truthy:
            return True
    if tags.get("operational_status", "").strip().lower() in truthy | {"vacant", "disused"}:
        return True
    for key in KINDS:
        active = tags.get(key)
        if active in {"closed", "vacant", "disused", "abandoned", "demolished"}:
            return True
        if active and any(tags.get(prefix + ":" + key) == active
                          for prefix in ("disused", "abandoned", "demolished", "razed", "closed")):
            return True
    try:
        if tags.get("end_date") and dt.date.fromisoformat(tags["end_date"]) <= today:
            return True
    except ValueError:
        pass  # An ambiguous source value is not evidence of permanent closure.
    return False


def supported(tags):
    return tags.get("amenity") in AMENITIES or tags.get("shop") in FOOD_SHOPS


def audit_relations(source, output, as_of=None):
    from relation_audit import audit
    result = audit(source, supported=supported, inactive=inactive, as_of=as_of)
    save(output, result)
    if result["affected_relation_count"]:
        print("::warning::Regional relation gaps: " + str(result["missing_unique_members"])
              + "; supported polygon candidates affected: "
              + str(len(result["blocked_polygon_relation_ids"])) + ". See relation-audit.json.", flush=True)
    return result


def tile(lon, lat, zoom):
    size = 2 ** zoom
    clipped = max(-85.05112878, min(85.05112878, lat))
    x = min(size - 1, max(0, math.floor((lon + 180) / 360 * size)))
    y = min(size - 1, max(0, math.floor((1 - math.asinh(math.tan(math.radians(clipped))) / math.pi) / 2 * size)))
    return f"{zoom}/{x}/{y}"


class InvalidGeometry(ValueError):
    """Unusable feature geometry, distinct from infrastructure/programming errors."""


def representative_geometry(geom, polygon):
    from shapely import make_valid
    from shapely.ops import unary_union

    if geom.is_empty:
        raise InvalidGeometry("Empty source geometry")
    repaired = not geom.is_valid
    if repaired:
        geom = make_valid(geom)
    if polygon:
        def polygons(value):
            if value.geom_type == "Polygon":
                return [value] if value.area > 0 else []
            if value.geom_type in {"MultiPolygon", "GeometryCollection"}:
                return [part for child in value.geoms for part in polygons(child)]
            return []
        parts = polygons(geom)
        if not parts:
            raise InvalidGeometry("Polygon collapsed to non-area geometry")
        geom = unary_union(parts) if len(parts) > 1 else parts[0]
        if geom.is_empty or not geom.is_valid or not math.isfinite(geom.area) or geom.area <= 0:
            raise InvalidGeometry("No valid polygon surface after repair")
    elif geom.is_empty or not math.isfinite(geom.length) or geom.length <= 0:
        raise InvalidGeometry("No valid line after repair")
    point = geom.representative_point()
    if point.is_empty or not math.isfinite(point.x) or not math.isfinite(point.y):
        raise InvalidGeometry("No finite representative point")
    return point, repaired


def export_records(source, dbpath, bbox=None, as_of=None, relation_audit=None,
                   quarantine_invalid_geometry=False, max_geometry_rejections=100):
    import osmium
    from shapely import from_wkb
    from shapely.errors import GEOSException
    from shapely.geometry import LineString, Polygon

    if not 1 <= max_geometry_rejections <= 1000:
        raise ValueError("Geometry rejection limit must be between 1 and 1000")
    dbpath.parent.mkdir(parents=True, exist_ok=True)
    dbpath.unlink(missing_ok=True)
    for name in ("geometry-report.json", "export-stats.json"):
        (dbpath.parent / name).unlink(missing_ok=True)
    today = as_of or dt.datetime.now(dt.timezone.utc).date()
    if relation_audit:
        audit = load(relation_audit)
        if audit["input_sha256"] != digest_file(source) or audit["closure_as_of"] != today.isoformat():
            raise ValueError("Relation audit does not match input/date")
    else:
        audit = audit_relations(source, dbpath.parent / "relation-audit.json", today)
    integrity = {key: audit[key] for key in ("missing_unique_members", "affected_relation_count", "blocked_polygon_relation_ids")}
    if audit["blocked_polygon_relation_ids"]:
        save(dbpath.parent / "export-stats.json", {"closure_as_of": today.isoformat(), "relation_integrity": integrity,
             "error": "incomplete_selected_polygon"})
        raise ValueError("Unassembled/incomplete selected relation(s): " + str(audit["blocked_polygon_relation_ids"][:20]))
    db = sqlite3.connect(dbpath)
    db.executescript("""
      CREATE TABLE features(id TEXT PRIMARY KEY, cell TEXT, parent TEXT, data TEXT, page TEXT);
      CREATE TABLE names(cell TEXT, term TEXT, id TEXT, PRIMARY KEY(cell,term,id));
      CREATE TABLE relations(id INTEGER PRIMARY KEY, assembled INTEGER NOT NULL);
      CREATE TABLE geometry_rejections(id TEXT PRIMARY KEY, data TEXT NOT NULL);
    """)
    factory = osmium.geom.WKBFactory()
    stats = {"selection_profile": SELECTION_PROFILE,
             "closed_candidates": 0, "unsupported_relations": 0, "repaired_geometries": 0,
             "relation_integrity": integrity}
    stats["closure_as_of"] = today.isoformat()
    stats["geometry_input_sha256"] = audit["input_sha256"]
    relation_names = {}
    completed = False

    class Handler(osmium.SimpleHandler):
        def eligible(self, obj):
            tags = {str(t.k): str(t.v) for t in obj.tags}
            if not tags.get("name", "").strip() or not supported(tags):
                return None
            if inactive(tags, today):
                stats["closed_candidates"] += 1
                return None
            return tags

        def reject(self, kind, osm_id, name, reason, message, area_id=None):
            feature_id = f"{kind}:{osm_id}"
            diagnostic = {"id": feature_id, "name": name[:512], "reason": reason,
                          "message": message[:500], "area_id": area_id}
            db.execute("INSERT OR REPLACE INTO geometry_rejections VALUES (?,?)",
                       (feature_id, encoded(diagnostic).decode()))
            # A closed way might already have emitted a fallback polygon. Never
            # retain it when its subsequently assembled area is rejected.
            db.execute("DELETE FROM features WHERE id=?", (feature_id,))
            if kind == "relation":
                db.execute("INSERT INTO relations VALUES (?,2) ON CONFLICT(id) DO UPDATE SET assembled=2", (osm_id,))
            count = db.execute("SELECT count(*) FROM geometry_rejections").fetchone()[0]
            if not quarantine_invalid_geometry:
                raise InvalidGeometry(f"Rejected {feature_id}: {reason}: {message}")
            if count > max_geometry_rejections:
                raise InvalidGeometry("Geometry quarantine limit exceeded; inspect geometry-report.json")
            if count <= 5:
                print(f"::warning::Quarantined {feature_id} ({reason}); see geometry-report.json", flush=True)

        def put(self, obj, kind, osm_id, tags, lon, lat, geometry, repaired=False):
            if not (math.isfinite(lon) and math.isfinite(lat) and -180 <= lon <= 180 and -90 <= lat <= 90):
                raise ValueError("Invalid geometry: " + kind + str(osm_id))
            feature_id = f"{kind}:{osm_id}"
            if db.execute("SELECT 1 FROM geometry_rejections WHERE id=?", (feature_id,)).fetchone():
                return
            if bbox and not (bbox[0] <= lon <= bbox[2] and bbox[1] <= lat <= bbox[3]):
                db.execute("DELETE FROM features WHERE id=?", (feature_id,))
                return
            name = tags["name"].strip()
            if len(name) > 512:
                raise ValueError("Oversized name")
            keys = set(KINDS) | {"name:en", "alt_name", "short_name", "official_name", "brand",
                                "cuisine", "website", "contact:website", "opening_hours"}
            kept = {k: v for k, v in tags.items() if k in keys or k.startswith("addr:")}
            if any(len(k) > 80 or len(v) > 4096 for k, v in kept.items()):
                raise ValueError("Oversized tag")
            record = {"id": feature_id, "name": name, "osm_version": int(obj.version),
                      "point": [round(lon, 7), round(lat, 7)], "tags": kept,
                      "geometry": geometry, "verification": "source_tags_only"}
            if repaired:
                record["geometry_repaired"] = True
            cell, parent = tile(lon, lat, 12), tile(lon, lat, 8)
            db.execute("INSERT INTO features VALUES (?,?,?,?,NULL) ON CONFLICT(id) DO UPDATE SET cell=excluded.cell,parent=excluded.parent,data=excluded.data",
                       (record["id"], cell, parent, encoded(record).decode()))

        def node(self, obj):
            tags = self.eligible(obj)
            if tags:
                self.put(obj, "node", obj.id, tags, obj.location.lon, obj.location.lat, "node")

        def way(self, obj):
            tags = self.eligible(obj)
            if not tags:
                return
            points = [(n.lon, n.lat) for n in obj.nodes]
            if len(points) < 2:
                self.reject("way", obj.id, tags["name"], "invalid_way_geometry", "Fewer than two nodes")
                return
            polygon = len(points) >= 4 and points[0] == points[-1] and tags.get("area") != "no"
            try:
                geom = Polygon(points) if polygon else LineString(points)
                point, repaired = representative_geometry(geom, polygon)
            except (GEOSException, InvalidGeometry) as exc:
                self.reject("way", obj.id, tags["name"], "invalid_way_geometry", str(exc))
                return
            stats["repaired_geometries"] += int(repaired)
            self.put(obj, "way", obj.id, tags, point.x, point.y,
                     "polygon_point" if polygon else "line_point", repaired)

        def relation(self, obj):
            tags = self.eligible(obj)
            if tags:
                if tags.get("type") in {"multipolygon", "boundary"}:
                    db.execute("INSERT OR IGNORE INTO relations VALUES (?,0)", (obj.id,))
                    relation_names[int(obj.id)] = tags["name"]
                else:
                    stats["unsupported_relations"] += 1

        def area(self, obj):
            tags = self.eligible(obj)
            if not tags:
                return
            kind, osm_id = ("way" if obj.from_way() else "relation"), int(obj.orig_id())
            try:
                raw = factory.create_multipolygon(obj)
            except RuntimeError as exc:
                # libosmium can reject an area before a WKB geometry exists.
                # Do not swallow unrelated runtime/infrastructure errors.
                if not str(exc).lower().startswith("invalid area"):
                    raise
                self.reject(kind, osm_id, tags["name"], "invalid_osmium_area", str(exc), int(obj.id))
                return
            try:
                point, repaired = representative_geometry(from_wkb(raw), True)
            except (GEOSException, InvalidGeometry) as exc:
                self.reject(kind, osm_id, tags["name"], "invalid_polygon_geometry", str(exc), int(obj.id))
                return
            stats["repaired_geometries"] += int(repaired)
            self.put(obj, kind, osm_id, tags, point.x, point.y, "polygon_point", repaired)
            if not obj.from_way():
                db.execute("INSERT INTO relations VALUES (?,1) ON CONFLICT(id) DO UPDATE SET assembled=1 WHERE assembled<>2", (osm_id,))

    try:
        Handler().apply_file(str(source), locations=True, idx="flex_mem")
        for (missing_id,) in db.execute("SELECT id FROM relations WHERE assembled=0").fetchall():
            Handler().reject("relation", missing_id, relation_names.get(missing_id, ""),
                             "unassembled_relation", "No complete area was assembled")
        # Assemble all geometries before indexing aliases: a polygon with holes
        # can move the representative point across a cell/bbox boundary.
        for osm_id, parent, raw in db.execute("SELECT id,parent,data FROM features ORDER BY id"):
            record = json.loads(raw)
            db.executemany("INSERT OR IGNORE INTO names VALUES (?,?,?)",
                           ((parent, term, osm_id) for term in aliases({**record["tags"], "name": record["name"]})))
        db.commit()
        stats["features"] = db.execute("SELECT count(*) FROM features").fetchone()[0]
        stats["name_entries"] = db.execute("SELECT count(*) FROM names").fetchone()[0]
        if not stats["features"]:
            raise ValueError("No features produced; empty builds require investigation")
        completed = True
    finally:
        try:
            rejected = [json.loads(row[0]) for row in db.execute("SELECT data FROM geometry_rejections ORDER BY id")]
            quality = "quarantined" if rejected else "complete"
            stats["geometry_quality"] = quality
            stats["geometry_rejected_count"] = len(rejected)
            stats["export_status"] = "success" if completed else "failed"
            save(dbpath.parent / "geometry-report.json", {
                "schema": 1, "status": stats["export_status"], "geometry_quality": quality,
                "input_sha256": audit["input_sha256"], "closure_as_of": today.isoformat(),
                "rejected_count": len(rejected), "rejected_features": rejected,
                "scope_note": "Regional input; rejected areas may be outside the requested bbox. No point is fabricated.",
            })
            save(dbpath.parent / "export-stats.json", stats)
        finally:
            db.close()


def blob(output, value):
    raw = encoded(value)
    if len(raw) > MAX_PAGE:
        raise ValueError("Page exceeds decoded byte limit")
    data = gzip.compress(raw, compresslevel=6, mtime=0)
    key = hashlib.sha256(data).hexdigest()
    path = output / "objects" / (key + ".json.gz")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return {"sha256": key, "bytes": len(data), "decoded_bytes": len(raw)}


def pages(output, kind, cell, rows, callback=None):
    header = {"schema": 1, "kind": kind, "cell": cell, "entries": []}
    batch, descriptors = [], []
    size = len(encoded(header))

    def flush():
        descriptor = blob(output, {**header, "entries": batch})
        if kind == "names":
            descriptor.update(first=batch[0][0], last=batch[-1][0])
        if callback:
            callback(batch, descriptor)
        descriptors.append(descriptor)

    for row in rows:
        row_size = len(encoded(row)) + 1
        if size + row_size > MAX_PAGE and batch:
            flush()
            batch, size = [], len(encoded(header))
        batch.append(row)
        size += row_size
    if batch:
        flush()
    return descriptors


def build_indexes(dbpath, output, source_info, header_info, scope):
    if output.exists() and any(output.iterdir()):
        raise ValueError("Output directory must be empty (prevent stale-file publication)")
    output.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(dbpath)
    db.executescript("CREATE INDEX IF NOT EXISTS features_cell ON features(cell,id); CREATE INDEX IF NOT EXISTS names_order ON names(cell,term,id);")
    directories = {"spatial": {}, "names": {}}
    for (cell,) in db.execute("SELECT DISTINCT cell FROM features ORDER BY cell").fetchall():
        def rows():
            for (raw,) in db.execute("SELECT data FROM features WHERE cell=? ORDER BY id", (cell,)):
                record = json.loads(raw)
                if not supported(record.get("tags", {})):
                    db.close()
                    raise ValueError("Non-food record in index input; rebuild from cached source: " + record["id"])
                yield record
        def locate(batch, descriptor):
            db.executemany("UPDATE features SET page=? WHERE id=?",
                           ((descriptor["sha256"], row["id"]) for row in batch))
        directories["spatial"][cell] = pages(output, "spatial", cell, rows(), locate)
    db.commit()
    for (cell,) in db.execute("SELECT DISTINCT cell FROM names ORDER BY cell").fetchall():
        def entries():
            for term, osm_id, record, page in db.execute("SELECT n.term,n.id,f.data,f.page FROM names n JOIN features f ON f.id=n.id WHERE n.cell=? ORDER BY n.term,n.id", (cell,)):
                data = json.loads(record)
                yield [term, osm_id, *data["point"], data["name"], page]
        directories["names"][cell] = pages(output, "names", cell, entries())
    # Directory pages are grouped by zoom-6 parent, so a client need not load a
    # state-wide directory. Oversized directory groups fail, never truncate.
    roots = {"spatial": {}, "names": {}}
    for kind, cells in directories.items():
        grouped = {}
        for cell, descriptors in cells.items():
            zoom, x, y = map(int, cell.split("/"))
            parent = f"6/{x >> (zoom - 6)}/{y >> (zoom - 6)}"
            # A dense city may have thousands of lexical pages. Keep each
            # descriptor in a separate directory row so one cell cannot make
            # an indivisible >64 KiB directory entry.
            grouped.setdefault(parent, []).extend([cell, [descriptor]] for descriptor in descriptors)
        for parent, entries in sorted(grouped.items()):
            descriptors = pages(output, "directory", parent, entries)
            roots[kind][parent] = descriptors
    count = db.execute("SELECT count(*) FROM features").fetchone()[0]
    db.close()
    manifest = {"schema": 1, "source": load(source_info), "pbf_header": load(header_info),
                "scope": scope, "feature_count": count, "max_decoded_page_bytes": MAX_PAGE,
                "selection": {"profile": SELECTION_PROFILE, "amenity": list(AMENITIES), "shop": list(FOOD_SHOPS)},
                "spatial_zoom": 12, "name_zoom": 8, "roots": roots,
                "attribution": "© OpenStreetMap contributors", "license": "ODbL-1.0",
                "exporter_revision": os.environ.get("GITHUB_SHA", "local"),
                "stats": load(dbpath.parent / "export-stats.json")}
    if len(encoded(manifest)) > 512 * 1024:
        raise ValueError("Manifest exceeds benchmark format limit; add another directory level")
    manifest["coverage"] = {"geometry_complete": manifest["stats"].get("geometry_rejected_count", 0) == 0,
                            "geometry_rejected_count": manifest["stats"].get("geometry_rejected_count", 0)}
    report = dbpath.parent / "geometry-report.json"
    if report.exists():
        shutil.copyfile(report, output / "geometry-report.json")
    save(output / "manifest.json", manifest)
    shutil.copyfile(ROOT / "DATA_LICENSE.md", output / "DATA_LICENSE.md")


def verify(output):
    manifest = load(output / "manifest.json")
    objects = {p.stem.split(".")[0]: p for p in (output / "objects").glob("*.json.gz")}
    from functools import lru_cache

    def read(key):
        with gzip.open(objects[key], "rb") as stream:
            raw = stream.read(MAX_PAGE + 1)
        if len(raw) > MAX_PAGE:
            raise ValueError("Decoded page exceeds limit")
        return json.loads(raw)

    feature_pages = {}
    for key, path in objects.items():
        if digest_file(path) != key:
            raise ValueError("Corrupt content-addressed object")
        page = read(key)
        if page["kind"] == "spatial":
            for record in page["entries"]:
                if record["id"] in feature_pages:
                    raise ValueError("Duplicate feature ID")
                feature_pages[record["id"]] = key
    # Never retain the decoded national/regional dataset in RAM for verification.
    read = lru_cache(maxsize=16)(read)
    reachable, spatial_ids, named_ids = set(), set(), set()
    last_name_by_cell = {}
    def visit(descriptor, expected):
        key = descriptor["sha256"]
        if key not in objects or read(key)["kind"] != expected:
            raise ValueError("Missing or wrong-kind page")
        path = objects[key]
        if path.stat().st_size != descriptor["bytes"] or len(encoded(read(key))) != descriptor["decoded_bytes"]:
            raise ValueError("Incorrect page size")
        reachable.add(key)
        return read(key)
    for kind, parents in manifest["roots"].items():
        for group in parents.values():
            for directory in group:
                page = visit(directory, "directory")
                for cell, descriptors in page["entries"]:
                    previous = last_name_by_cell.get(cell)
                    for descriptor in descriptors:
                        data = visit(descriptor, kind)
                        if kind == "names" and (not data["entries"] or descriptor["first"] != data["entries"][0][0] or descriptor["last"] != data["entries"][-1][0]):
                            raise ValueError("Invalid lexical directory bounds")
                        if data["cell"] != cell:
                            raise ValueError("Wrong cell")
                        for entry in data["entries"]:
                            if kind == "spatial":
                                if entry["id"] in spatial_ids:
                                    raise ValueError("Duplicate feature ID")
                                spatial_ids.add(entry["id"])
                            else:
                                order = (entry[0], entry[1])
                                if previous is not None and order <= previous:
                                    raise ValueError("Unsorted/duplicate name posting")
                                previous = order
                                last_name_by_cell[cell] = order
                                if feature_pages.get(entry[1]) != entry[5]:
                                    raise ValueError("Broken record locator")
                                named_ids.add(entry[1])
    geometry = load(output / "geometry-report.json")
    rejected = {row["id"] for row in geometry["rejected_features"]}
    count = geometry["rejected_count"]
    if (len(rejected) != count or len(geometry["rejected_features"]) != count
            or manifest["stats"]["geometry_rejected_count"] != count
            or manifest["coverage"]["geometry_rejected_count"] != count
            or manifest["coverage"]["geometry_complete"] != (count == 0)
            or geometry["status"] != "success" or manifest["stats"]["export_status"] != "success"
            or geometry["input_sha256"] != manifest["stats"]["geometry_input_sha256"]
            or geometry["closure_as_of"] != manifest["stats"]["closure_as_of"]
            or geometry["geometry_quality"] != ("quarantined" if count else "complete")
            or rejected & spatial_ids):
        raise ValueError("Geometry quality/report mismatch or rejected feature was indexed")
    if spatial_ids != named_ids or len(spatial_ids) != manifest["feature_count"] or reachable != set(objects):
        raise ValueError("Manifest coverage/reachability mismatch")
    return {"features": len(spatial_ids), "objects": len(objects),
            "geometry_rejected_count": count, "geometry_complete": count == 0,
            "stored_bytes": sum(p.stat().st_size for p in output.rglob("*") if p.is_file())}


def require_complete_geometry(output):
    manifest = load(output / "manifest.json")
    if manifest["stats"]["geometry_rejected_count"] or not manifest["coverage"]["geometry_complete"]:
        raise ValueError("Public release blocked: quarantined geometry; inspect geometry-report.json before promotion")


def package(output, bundle):
    stats = verify(output)
    bundle.parent.mkdir(parents=True, exist_ok=True)
    with bundle.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
                for path in sorted(output.rglob("*")):
                    if path.is_file():
                        info = archive.gettarinfo(str(path), arcname=path.relative_to(output).as_posix())
                        info.mtime = info.uid = info.gid = 0
                        info.uname = info.gname = ""
                        with path.open("rb") as stream:
                            archive.addfile(info, stream)
    if bundle.stat().st_size >= 2 * 1024**3:
        raise ValueError("Bundle exceeds release asset size limit")
    save(bundle.with_suffix(bundle.suffix + ".json"), {**stats, "bundle_bytes": bundle.stat().st_size,
         "sha256": digest_file(bundle)})


def process_rss(pid):
    total = 0
    todo = [pid]
    while todo:
        current = todo.pop()
        try:
            text = Path(f"/proc/{current}/status").read_text()
            match = re.search(r"^VmRSS:\s+(\d+)", text, re.M)
            total += int(match[1]) * 1024 if match else 0
            todo.extend(map(int, Path(f"/proc/{current}/task/{current}/children").read_text().split()))
        except (OSError, ValueError):
            pass
    return total


def measure(stage, metrics, seconds, command):
    started = time.monotonic()
    proc = subprocess.Popen(command, start_new_session=True)
    rss, disk_used, min_free = 0, 0, shutil.disk_usage(".").free
    next_heartbeat = started + 30
    timed_out = False
    resource_limit = None
    try:
        while True:
            rss = max(rss, process_rss(proc.pid))
            disk = shutil.disk_usage(".")
            disk_used, min_free = max(disk_used, disk.used), min(min_free, disk.free)
            if time.monotonic() >= next_heartbeat:
                print(f"Stage {stage}: {time.monotonic() - started:.0f}s elapsed; "
                      f"RSS {rss / 1024**2:.0f} MiB; free disk {disk.free / 1024**3:.1f} GiB", flush=True)
                next_heartbeat = time.monotonic() + 30
            try:
                code = proc.wait(timeout=.25)
                break
            except subprocess.TimeoutExpired:
                if rss > 12 * 1024**3 or disk.free < 1024**3:
                    resource_limit = "rss_or_disk_headroom"
                if time.monotonic() - started > seconds or resource_limit:
                    timed_out = time.monotonic() - started > seconds
                    os.killpg(proc.pid, signal.SIGTERM)
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(proc.pid, signal.SIGKILL)
                        proc.wait()
                    code = 124
                    break
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
        data = load(metrics) if metrics.exists() else {}
        data[stage] = {"elapsed_seconds": round(time.monotonic() - started, 3),
                       "sampled_peak_process_tree_rss_bytes": rss,
                       "sampled_filesystem_used_high_water_bytes": disk_used,
                       "sampled_filesystem_min_free_bytes": min_free,
                       "exit_code": proc.returncode, "timed_out": timed_out,
                       "resource_limit": resource_limit}
        save(metrics, data)
    return code


def report(work, destination):
    result = {"schema": 1, "revision": os.environ.get("GITHUB_SHA", "local"),
              "runner_os": sys.platform, "stages": load(work / "metrics.json") if (work / "metrics.json").exists() else {}}
    for name in ("source", "cache-state", "export-stats", "relation-audit", "geometry-report"):
        if (work / (name + ".json")).exists():
            result[name] = load(work / (name + ".json"))
    save(destination, result)
    lines = ["### Regional place-index measurements", "", "| Stage | Seconds | Sampled peak RSS MiB | Minimum free disk GiB | Exit |", "|---|---:|---:|---:|---:|"]
    for name, item in result["stages"].items():
        lines.append(f"| {name} | {item['elapsed_seconds']} | {item['sampled_peak_process_tree_rss_bytes']/1024**2:.1f} | {item['sampled_filesystem_min_free_bytes']/1024**3:.2f} | {item['exit_code']} |")
    cache = result.get("cache-state")
    if cache:
        lines.extend(["", "Source: mode=" + str(cache.get("mode", "unknown"))
                      + "; result=" + str(cache.get("result", cache.get("status")))
                      + "; full_download=" + str(cache.get("full_download_performed", False))
                      + "; persistence=" + str(cache.get("persistence", "restored/unchanged" if cache.get("cache_hit") and not cache.get("cache_changed") else "not_confirmed"))])
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as stream:
            stream.write("\n".join(lines) + "\n")
    print("\n".join(lines))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--benchmark", default="seattle")
    p.add_argument("--regions", default="washington")
    p = sub.add_parser("download")
    p.add_argument("--region", required=True)
    p.add_argument("--work", type=Path, required=True)
    p = sub.add_parser("prepare-source")
    p.add_argument("--region", required=True)
    p.add_argument("--work", type=Path, required=True)
    p.add_argument("--mode", choices=["auto", "reuse", "update", "fresh", "seed"], default="auto")
    p.set_defaults(allow_full_download=True)
    permission = p.add_mutually_exclusive_group()
    permission.add_argument("--allow-full-download", action="store_true", dest="allow_full_download")
    permission.add_argument("--no-full-download", action="store_false", dest="allow_full_download")
    p.add_argument("--seed-file", type=Path)
    p.add_argument("--seed-sha256")
    p = sub.add_parser("export")
    p.add_argument("--input", type=Path, required=True)
    p.add_argument("--db", type=Path, required=True)
    p.add_argument("--bbox", default="")
    p.add_argument("--as-of", help="UTC date for reproducible end_date filtering (YYYY-MM-DD)")
    p.add_argument("--relation-audit", type=Path)
    p.add_argument("--quarantine-invalid-geometry", action="store_true", help="Benchmark valid records, report exclusions; not complete coverage")
    p.add_argument("--max-geometry-rejections", type=int, default=100)
    p = sub.add_parser("index")
    p.add_argument("--db", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--source", type=Path, required=True)
    p.add_argument("--header", type=Path, required=True)
    p.add_argument("--scope", required=True)
    p = sub.add_parser("package")
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--bundle", type=Path, required=True)
    p = sub.add_parser("verify")
    p.add_argument("--output", type=Path, required=True)
    p = sub.add_parser("require-complete-geometry")
    p.add_argument("--output", type=Path, required=True)
    p = sub.add_parser("audit-relations")
    p.add_argument("--input", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    sub.add_parser("selectors")
    p = sub.add_parser("measure")
    p.add_argument("--stage", required=True)
    p.add_argument("--metrics", type=Path, required=True)
    p.add_argument("--seconds", type=int, default=2100)
    p.add_argument("command", nargs=argparse.REMAINDER)
    p = sub.add_parser("report")
    p.add_argument("--work", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.action == "plan":
        print(encoded(plan(args.benchmark, args.regions)).decode())
    elif args.action == "download":
        download(args.region, args.work)
    elif args.action == "prepare-source":
        from source_cache import prepare
        url = load(ROOT / "regions.json")["regions"][args.region]
        state = prepare(args.region, url, args.work, args.mode, args.allow_full_download,
                        full_download=download, seed_file=args.seed_file, seed_sha256=args.seed_sha256)
        outputs = os.environ.get("GITHUB_OUTPUT")
        if outputs:
            with open(outputs, "a") as stream:
                print("cache_changed=" + str(state["cache_changed"]).lower(), file=stream)
                print("cache_ready=true", file=stream)
    elif args.action == "require-complete-geometry":
        require_complete_geometry(args.output)
    elif args.action == "audit-relations":
        audit_relations(args.input, args.output)
    elif args.action == "selectors":
        print("\n".join(SELECTORS))
    elif args.action == "export":
        bbox = tuple(map(float, args.bbox.split(","))) if args.bbox else None
        if bbox and (len(bbox) != 4 or not all(math.isfinite(n) for n in bbox)
                     or not -180 <= bbox[0] < bbox[2] <= 180 or not -90 <= bbox[1] < bbox[3] <= 90):
            raise ValueError("Expected west,south,east,north bbox (no dateline wrap)")
        export_records(args.input, args.db, bbox, dt.date.fromisoformat(args.as_of) if args.as_of else None,
                       args.relation_audit, args.quarantine_invalid_geometry, args.max_geometry_rejections)
    elif args.action == "index":
        build_indexes(args.db, args.output, args.source, args.header, args.scope)
    elif args.action == "package":
        package(args.output, args.bundle)
    elif args.action == "verify":
        print(encoded(verify(args.output)).decode())
    elif args.action == "measure":
        command = args.command[1:] if args.command[:1] == ["--"] else args.command
        return measure(args.stage, args.metrics, args.seconds, command)
    elif args.action == "report":
        report(args.work, args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
