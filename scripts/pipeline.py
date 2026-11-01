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
AMENITIES = (
    "restaurant", "fast_food", "cafe", "bar", "pub", "food_court", "ice_cream",
    "marketplace", "pharmacy", "hospital", "clinic", "doctors", "dentist", "bank",
    "library", "school", "college", "university", "post_office", "community_centre",
    "place_of_worship", "cinema", "theatre", "townhall", "bus_station", "ferry_terminal",
)
SELECTORS = [
    "nwr/amenity=" + ",".join(AMENITIES), "nwr/shop", "nwr/tourism",
    "nwr/leisure=park,fitness_centre,sports_centre,swimming_pool,stadium",
    "nwr/railway=station,halt", "nwr/aeroway=aerodrome,terminal",
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
        try:
            with opener.open(urllib.request.Request(url + ".md5", headers=headers), timeout=30) as reply:
                sidecar = reply.read(4097)
            match = re.match(rb"^([a-fA-F0-9]{32})(?:\s|$)", sidecar)
            if len(sidecar) > 4096 or not match:
                raise ValueError("Invalid checksum sidecar")
            md5 = hashlib.md5(usedforsecurity=False)
            sha256 = hashlib.sha256()
            size = 0
            with opener.open(urllib.request.Request(url, headers=headers), timeout=30) as reply:
                declared = int(reply.headers.get("Content-Length", 0))
                if declared > max_bytes:
                    raise ValueError("Source exceeds download size limit")
                with temporary.open("wb") as output:
                    while True:
                        if time.monotonic() >= deadline:
                            raise TimeoutError("Total download budget exceeded")
                        chunk = reply.read(1024 * 1024)
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > max_bytes or shutil.disk_usage(work).free < 2_000_000_000:
                            raise ValueError("Download size/disk reserve exceeded")
                        output.write(chunk)
                        md5.update(chunk)
                        sha256.update(chunk)
            if not size or md5.hexdigest() != match[1].decode().lower():
                raise ValueError("Source checksum mismatch; latest may have changed")
            temporary.replace(target)
            save(work / "source.json", {"region": region, "url": url, "bytes": size,
                 "sha256": sha256.hexdigest(), "md5": md5.hexdigest(), "checksum_verified": True})
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
    return (tags.get("amenity") in AMENITIES or bool(tags.get("shop"))
            or bool(tags.get("tourism"))
            or tags.get("leisure") in {"park", "fitness_centre", "sports_centre", "swimming_pool", "stadium"}
            or tags.get("railway") in {"station", "halt"}
            or tags.get("aeroway") in {"aerodrome", "terminal"})


def tile(lon, lat, zoom):
    size = 2 ** zoom
    clipped = max(-85.05112878, min(85.05112878, lat))
    x = min(size - 1, max(0, math.floor((lon + 180) / 360 * size)))
    y = min(size - 1, max(0, math.floor((1 - math.asinh(math.tan(math.radians(clipped))) / math.pi) / 2 * size)))
    return f"{zoom}/{x}/{y}"


def export_records(source, dbpath, bbox=None, as_of=None):
    import osmium
    from shapely import from_wkb, make_valid
    from shapely.geometry import LineString, Polygon

    dbpath.parent.mkdir(parents=True, exist_ok=True)
    dbpath.unlink(missing_ok=True)
    db = sqlite3.connect(dbpath)
    db.executescript("""
      CREATE TABLE features(id TEXT PRIMARY KEY, cell TEXT, parent TEXT, data TEXT, page TEXT);
      CREATE TABLE names(cell TEXT, term TEXT, id TEXT, PRIMARY KEY(cell,term,id));
      CREATE TABLE relations(id INTEGER PRIMARY KEY, assembled INTEGER NOT NULL);
    """)
    factory = osmium.geom.WKBFactory()
    stats = {"closed_candidates": 0, "unsupported_relations": 0, "repaired_geometries": 0}
    today = as_of or dt.datetime.now(dt.timezone.utc).date()
    stats["closure_as_of"] = today.isoformat()

    class Handler(osmium.SimpleHandler):
        def eligible(self, obj):
            tags = {str(t.k): str(t.v) for t in obj.tags}
            if not tags.get("name", "").strip() or not supported(tags):
                return None
            if inactive(tags, today):
                stats["closed_candidates"] += 1
                return None
            return tags

        def put(self, obj, kind, osm_id, tags, lon, lat, geometry):
            if not (math.isfinite(lon) and math.isfinite(lat) and -180 <= lon <= 180 and -90 <= lat <= 90):
                raise ValueError("Invalid geometry: " + kind + str(osm_id))
            feature_id = f"{kind}:{osm_id}"
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
                raise ValueError("Selected way has incomplete geometry")
            polygon = len(points) >= 4 and points[0] == points[-1] and tags.get("area") != "no"
            geom = Polygon(points) if polygon else LineString(points)
            if not geom.is_valid:
                geom = make_valid(geom)
                stats["repaired_geometries"] += 1
            if geom.is_empty:
                raise ValueError("Empty selected geometry")
            point = geom.representative_point()
            self.put(obj, "way", obj.id, tags, point.x, point.y, "polygon_point" if polygon else "line_point")

        def relation(self, obj):
            tags = self.eligible(obj)
            if tags:
                if tags.get("type") in {"multipolygon", "boundary"}:
                    db.execute("INSERT OR IGNORE INTO relations VALUES (?,0)", (obj.id,))
                else:
                    stats["unsupported_relations"] += 1

        def area(self, obj):
            tags = self.eligible(obj)
            if not tags:
                return
            if not obj.from_way():
                db.execute("INSERT INTO relations VALUES (?,1) ON CONFLICT(id) DO UPDATE SET assembled=1", (obj.orig_id(),))
            geom = from_wkb(factory.create_multipolygon(obj))
            if not geom.is_valid:
                geom = make_valid(geom)
                stats["repaired_geometries"] += 1
            if geom.is_empty:
                raise ValueError("Empty assembled polygon")
            point = geom.representative_point()
            self.put(obj, "way" if obj.from_way() else "relation", obj.orig_id(), tags,
                     point.x, point.y, "polygon_point")

    try:
        Handler().apply_file(str(source), locations=True, idx="flex_mem")
        missing = db.execute("SELECT id FROM relations WHERE assembled=0 LIMIT 1").fetchone()
        if missing:
            raise ValueError(f"Unassembled selected relation {missing[0]}")
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
        rows = (json.loads(row[0]) for row in db.execute("SELECT data FROM features WHERE cell=? ORDER BY id", (cell,)))
        def locate(batch, descriptor):
            db.executemany("UPDATE features SET page=? WHERE id=?",
                           ((descriptor["sha256"], row["id"]) for row in batch))
        directories["spatial"][cell] = pages(output, "spatial", cell, rows, locate)
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
                "spatial_zoom": 12, "name_zoom": 8, "roots": roots,
                "attribution": "© OpenStreetMap contributors", "license": "ODbL-1.0",
                "exporter_revision": os.environ.get("GITHUB_SHA", "local"),
                "stats": load(dbpath.parent / "export-stats.json")}
    if len(encoded(manifest)) > 512 * 1024:
        raise ValueError("Manifest exceeds benchmark format limit; add another directory level")
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
    if spatial_ids != named_ids or len(spatial_ids) != manifest["feature_count"] or reachable != set(objects):
        raise ValueError("Manifest coverage/reachability mismatch")
    return {"features": len(spatial_ids), "objects": len(objects),
            "stored_bytes": sum(p.stat().st_size for p in output.rglob("*") if p.is_file())}


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
    timed_out = False
    resource_limit = None
    try:
        while True:
            rss = max(rss, process_rss(proc.pid))
            disk = shutil.disk_usage(".")
            disk_used, min_free = max(disk_used, disk.used), min(min_free, disk.free)
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
    for name in ("source", "export-stats"):
        if (work / (name + ".json")).exists():
            result[name] = load(work / (name + ".json"))
    save(destination, result)
    lines = ["### Regional place-index measurements", "", "| Stage | Seconds | Sampled peak RSS MiB | Minimum free disk GiB | Exit |", "|---|---:|---:|---:|---:|"]
    for name, item in result["stages"].items():
        lines.append(f"| {name} | {item['elapsed_seconds']} | {item['sampled_peak_process_tree_rss_bytes']/1024**2:.1f} | {item['sampled_filesystem_min_free_bytes']/1024**3:.2f} | {item['exit_code']} |")
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
    p = sub.add_parser("export")
    p.add_argument("--input", type=Path, required=True)
    p.add_argument("--db", type=Path, required=True)
    p.add_argument("--bbox", default="")
    p.add_argument("--as-of", help="UTC date for reproducible end_date filtering (YYYY-MM-DD)")
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
    elif args.action == "selectors":
        print("\n".join(SELECTORS))
    elif args.action == "export":
        bbox = tuple(map(float, args.bbox.split(","))) if args.bbox else None
        if bbox and (len(bbox) != 4 or not all(math.isfinite(n) for n in bbox)
                     or not -180 <= bbox[0] < bbox[2] <= 180 or not -90 <= bbox[1] < bbox[3] <= 90):
            raise ValueError("Expected west,south,east,north bbox (no dateline wrap)")
        export_records(args.input, args.db, bbox, dt.date.fromisoformat(args.as_of) if args.as_of else None)
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
