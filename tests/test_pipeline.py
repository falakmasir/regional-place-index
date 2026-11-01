"""Synthetic, isolated tests. Fixtures are never published as live place data."""
import datetime as dt
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import pipeline as p


class Rules(unittest.TestCase):
    def test_plans(self):
        self.assertEqual(p.plan("seattle", "ignored")["include"][0]["region"], "washington")
        self.assertEqual(len(p.plan("new-york", "ignored")["include"]), 2)
        self.assertEqual(len(p.plan("all-benchmarks", "ignored")["include"]), 5)
        self.assertEqual(len(p.plan("full-regions", "all")["include"]), 51)
        self.assertEqual(len(p.plan("full-regions", "washington,california")["include"]), 2)
        for bad in ("", "washington,washington", "../../escape", "x;echo bad"):
            with self.subTest(bad=bad), self.assertRaises((ValueError, KeyError)):
                p.plan("full-regions", bad)

    def test_closure_not_website_or_old_business(self):
        today = dt.date(2026, 10, 31)
        for tags in ({"closed": "yes"}, {"disused": "true"}, {"operational_status": "closed"},
                     {"amenity": "restaurant", "disused:amenity": "restaurant"}, {"end_date": "2020-01-01"}):
            self.assertTrue(p.inactive(tags, today), tags)
        for tags in ({"website": "https://closed.invalid"}, {"end_date": "unknown"},
                     {"end_date": "2030-01-01"}, {"amenity": "restaurant", "disused:amenity": "pub"}):
            self.assertFalse(p.inactive(tags, today), tags)

    def test_aliases_and_tile_boundaries(self):
        self.assertEqual(p.normalize("  CAFÉ  Test  "), "cafe test")
        self.assertIn("test", p.aliases({"name": "Café Test"}))
        self.assertTrue(p.normalize("東京"))
        self.assertIn("zzz primary", p.aliases({"name": "Zzz Primary", "alt_name": "aa bb cc dd;ee ff gg hh;ii jj kk ll;mm nn oo pp"}))
        self.assertLessEqual(len(p.aliases({"name": " ".join(map(str, range(100)))})), 16)
        self.assertEqual(p.tile(180, 90, 12), "12/4095/0")
        self.assertTrue(p.tile(-180, -90, 12).startswith("12/0/"))
        self.assertTrue(p.supported({"shop": "supermarket", "brand": "Any Chain"}))
        self.assertFalse(p.supported({"highway": "residential"}))

    def test_source_urls(self):
        for url in ("http://download.geofabrik.de/file", "https://example.org/file",
                    "https://download.geofabrik.de@evil.invalid/x", "https://download.geofabrik.de:444/x"):
            with self.assertRaises(ValueError):
                p.check_url(url)
        p.check_url("https://download.geofabrik.de/north-america/us/washington-latest.osm.pbf")


class Download(unittest.TestCase):
    @staticmethod
    def reply(body):
        stream = io.BytesIO(body)
        stream.headers = {"Content-Length": str(len(body))}
        return stream

    def test_checksum_and_source_provenance(self):
        data = b"test download bytes, not a real PBF"
        checksum = hashlib.md5(data).hexdigest().encode() + b"  source.osm.pbf\n"
        opener = unittest.mock.Mock()
        opener.open.side_effect = [self.reply(checksum), self.reply(data)]
        with tempfile.TemporaryDirectory() as folder, patch.object(p.urllib.request, "build_opener", return_value=opener):
            work = Path(folder)
            p.download("washington", work)
            self.assertEqual((work / "source.osm.pbf").read_bytes(), data)
            source = p.load(work / "source.json")
            self.assertTrue(source["checksum_verified"])
            self.assertEqual(source["sha256"], hashlib.sha256(data).hexdigest())

    def test_bad_checksum_never_promotes_partial_download(self):
        opener = unittest.mock.Mock()
        opener.open.side_effect = [self.reply(body) for _ in range(2) for body in (b"0" * 32 + b"  source\n", b"bad")]
        with tempfile.TemporaryDirectory() as folder, patch.object(p.urllib.request, "build_opener", return_value=opener), patch.object(p.time, "sleep"):
            with self.assertRaises(ValueError):
                p.download("washington", Path(folder))
            self.assertFalse((Path(folder) / "source.osm.pbf").exists())
            self.assertFalse((Path(folder) / "source.partial").exists())

    def test_size_limit(self):
        opener = unittest.mock.Mock()
        opener.open.side_effect = [self.reply(body) for _ in range(2) for body in (b"0" * 32, b"large")]
        with tempfile.TemporaryDirectory() as folder, patch.object(p.urllib.request, "build_opener", return_value=opener), patch.object(p.time, "sleep"):
            with self.assertRaisesRegex(ValueError, "size limit"):
                p.download("washington", Path(folder), max_bytes=2)


class Export(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.db = self.work / "records.sqlite"
        self.output = self.work / "output"

    def records(self):
        with sqlite3.connect(self.db) as db:
            return {row[0]: json.loads(row[1]) for row in db.execute("SELECT id,data FROM features")}

    def seed(self, bbox=None):
        # Real libosmium/shapely parsing, not geometry/provider mocks.
        p.export_records(ROOT / "tests/fixtures/places.osm", self.db, bbox)

    def index(self):
        p.save(self.work / "source.json", {"checksum_verified": True, "sha256": "fixture", "region": "fixture"})
        p.save(self.work / "header.json", {"fixture": True})
        p.build_indexes(self.db, self.output, self.work / "source.json", self.work / "header.json", "unit-test")

    def test_typed_ids_closure_chain_and_scope(self):
        self.seed((-122.46, 47.48, -122.20, 47.75))
        records = self.records()
        self.assertEqual(set(records), {"node:1", "node:3", "node:5", "way:1", "relation:700"})
        self.assertEqual(records["node:1"]["osm_version"], 3)
        self.assertEqual(records["relation:700"]["osm_version"], 5)
        self.assertEqual(records["node:1"]["tags"]["brand"], "Large Chain")
        self.assertTrue(all(x["verification"] == "source_tags_only" for x in records.values()))
        self.assertEqual(p.load(self.work / "export-stats.json")["unsupported_relations"], 1)

    def test_representative_point_is_inside_concavity_and_not_hole(self):
        from shapely.geometry import Polygon, Point
        self.seed()
        records = self.records()
        outer = Polygon([(-122.36,47.60),(-122.34,47.60),(-122.34,47.62),(-122.36,47.62)],
                        [[(-122.355,47.605),(-122.345,47.605),(-122.345,47.615),(-122.355,47.615)]])
        self.assertTrue(outer.contains(Point(records["relation:700"]["point"])))
        concave = Polygon([(-122.40,47.60),(-122.38,47.60),(-122.38,47.62),(-122.39,47.61),(-122.40,47.62)])
        self.assertTrue(concave.covers(Point(records["way:1"]["point"])))

    def test_deterministic_bounded_pages_and_bundle(self):
        self.seed()
        # Tiny pages simulate dense-city multi-page directories without a large
        # fixture. One cell must be able to span directory pages safely.
        with patch.object(p, "MAX_PAGE", 512):
            self.index()
            p.verify(self.output)
            roots = p.load(self.output / "manifest.json")["roots"]
            self.assertTrue(any(len(group) > 1 for group in roots["names"].values()))
        stats = p.verify(self.output)
        self.assertEqual(stats["features"], 6)
        first = self.work / "first.tar.gz"
        second = self.work / "second.tar.gz"
        p.package(self.output, first)
        p.package(self.output, second)
        self.assertEqual(first.read_bytes(), second.read_bytes())
        with tarfile.open(first) as archive:
            self.assertIn("DATA_LICENSE.md", archive.getnames())
            self.assertFalse(any(n.endswith((".sqlite", ".osm", ".pbf")) for n in archive.getnames()))
        for file in (self.output / "objects").glob("*.gz"):
            self.assertLessEqual(len(gzip.decompress(file.read_bytes())), p.MAX_PAGE)

    def test_real_pbf_roundtrip(self):
        import osmium
        target = self.work / "roundtrip.osm.pbf"
        with osmium.SimpleWriter(str(target)) as writer:
            class Copy(osmium.SimpleHandler):
                def node(self, obj): writer.add_node(obj)
                def way(self, obj): writer.add_way(obj)
                def relation(self, obj): writer.add_relation(obj)
            Copy().apply_file(str(ROOT / "tests/fixtures/places.osm"))
        p.export_records(target, self.db)
        self.assertEqual(len(self.records()), 6)
        self.assertIn("relation:700", self.records())

    def test_corruption_rejected(self):
        self.seed()
        self.index()
        file = next((self.output / "objects").glob("*.gz"))
        file.write_bytes(b"corrupt")
        with self.assertRaisesRegex(ValueError, "Corrupt"):
            p.verify(self.output)

    def test_missing_object_rejected(self):
        self.seed()
        self.index()
        next((self.output / "objects").glob("*.gz")).unlink()
        with self.assertRaises(ValueError):
            p.verify(self.output)

    def test_empty_and_stale_output_rejected(self):
        with self.assertRaisesRegex(ValueError, "No features"):
            self.seed((0, 0, 1, 1))
        self.seed()
        self.index()
        with self.assertRaisesRegex(ValueError, "empty"):
            self.index()

    def test_unassembled_relation_rejected_even_in_scoped_run(self):
        source = self.work / "broken.osm"
        source.write_text('<osm version="0.6"><relation id="999" version="1"><member type="way" ref="99" role="outer"/><tag k="type" v="multipolygon"/><tag k="amenity" v="restaurant"/><tag k="name" v="Broken"/></relation></osm>')
        with self.assertRaisesRegex(ValueError, "Unassembled"):
            p.export_records(source, self.db, (-122.46, 47.48, -122.20, 47.75))

    def test_aliases_follow_final_polygon_point_and_scope(self):
        self.seed()
        with sqlite3.connect(self.db) as db:
            for cell, record in db.execute("SELECT n.cell,f.data FROM names n JOIN features f ON f.id=n.id"):
                lon, lat = json.loads(record)["point"]
                self.assertEqual(cell, p.tile(lon, lat, 8))

    @unittest.skipUnless(shutil.which("osmium"), "native osmium-tool is installed by hosted CI")
    def test_native_reference_preserving_filter(self):
        target = self.work / "filtered.osm.pbf"
        subprocess.run(["osmium", "tags-filter", str(ROOT / "tests/fixtures/places.osm"), *p.SELECTORS, "-o", str(target)], check=True)
        subprocess.run(["osmium", "check-refs", "-r", str(target)], check=True)
        p.export_records(target, self.db)
        self.assertIn("relation:700", self.records())


class BoundsAndMetrics(unittest.TestCase):
    def test_pages_split_and_oversize_reject(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder)
            descriptors = p.pages(output, "names", "8/1/1", [[str(i), "x" * 10000] for i in range(30)])
            self.assertGreater(len(descriptors), 1)
            self.assertTrue(all(d["decoded_bytes"] <= p.MAX_PAGE for d in descriptors))
            with self.assertRaisesRegex(ValueError, "limit"):
                p.pages(output, "names", "8/1/1", [["x", "y" * 70000]])

    def test_measure_records_success_failure_and_timeout(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "metrics.json"
            self.assertEqual(p.measure("ok", path, 5, [sys.executable, "-c", "pass"]), 0)
            self.assertEqual(p.measure("bad", path, 5, [sys.executable, "-c", "raise SystemExit(3)"]), 3)
            self.assertEqual(p.measure("timeout", path, .05, [sys.executable, "-c", "import time; time.sleep(60)"]), 124)
            self.assertTrue(p.load(path)["timeout"]["timed_out"])
            self.assertGreater(p.load(path)["ok"]["sampled_filesystem_min_free_bytes"], 0)
            p.report(Path(folder), Path(folder) / "summary.json")
            self.assertEqual(set(p.load(Path(folder) / "summary.json")["stages"]), {"ok", "bad", "timeout"})


class Workflow(unittest.TestCase):
    def test_workflow_boundaries_and_opt_ins(self):
        import yaml
        path = ROOT / ".github/workflows/benchmark.yml"
        workflow = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
        self.assertIn("workflow_dispatch", workflow["on"])
        self.assertEqual(workflow["on"]["workflow_dispatch"]["inputs"]["publish_release"]["default"], "false")
        self.assertIn("ENABLE_DAILY_BUILDS", workflow["jobs"]["plan"]["if"])
        self.assertEqual(workflow["jobs"]["build"]["strategy"]["max-parallel"], "2")
        self.assertLessEqual(int(workflow["jobs"]["build"]["timeout-minutes"]), 360)
        for job in workflow["jobs"].values():
            for step in job["steps"]:
                if "uses" in step:
                    self.assertRegex(step["uses"], r"^actions/[a-z-]+@[a-f0-9]{40}$")
        self.assertNotIn("secrets.", path.read_text())
        self.assertNotIn("curl ", path.read_text())
        self.assertIn("--draft=false", workflow["jobs"]["publish"]["steps"][0]["run"])


if __name__ == "__main__":
    unittest.main()
