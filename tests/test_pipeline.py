"""Synthetic, isolated tests. Fixtures are never published as live place data."""
import datetime as dt
import gzip
import hashlib
import io
import http.server
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import ssl
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import pipeline as p
import source_cache as c


class Rules(unittest.TestCase):
    def test_plans(self):
        self.assertEqual(p.plan("seattle", "ignored")["include"][0]["region"], "washington")
        # New York and Miami are split into 2x2 tiles; every tile is one build job.
        self.assertEqual(len(p.plan("new-york", "ignored")["include"]), 8)
        self.assertEqual(len(p.plan("miami", "ignored")["include"]), 4)
        self.assertEqual(len(p.plan("all-benchmarks", "ignored")["include"]), 14)
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

    def test_food_allowlist_accepts_food_venues_and_retail_only(self):
        amenities = ("restaurant", "fast_food", "cafe", "bar", "pub", "food_court", "ice_cream", "marketplace")
        shops = ("bakery", "supermarket", "convenience", "grocery", "greengrocer", "deli",
                 "butcher", "seafood", "fishmonger", "cheese", "dairy", "farm", "frozen_food",
                 "health_food", "pasta", "pastry", "confectionery", "chocolate", "coffee", "tea",
                 "spices", "nuts", "food")
        self.assertEqual(set(p.AMENITIES), set(amenities))
        self.assertEqual(set(p.FOOD_SHOPS), set(shops))
        for key, values in (("amenity", amenities), ("shop", shops)):
            for value in values:
                with self.subTest(key=key, value=value):
                    self.assertTrue(p.supported({key: value, "name": "Any Chain"}))
        excluded = {"amenity": ("bank", "school", "college", "university", "hospital", "clinic",
                                "pharmacy", "library", "fuel", "bus_station", "ferry_terminal"),
                    "shop": ("yes", "clothes", "car", "electronics", "general", "vacant"),
                    "tourism": ("hotel", "camp_site", "museum"),
                    "leisure": ("park", "sports_centre"),
                    "railway": ("station",), "aeroway": ("terminal",)}
        for key, values in excluded.items():
            for value in values:
                with self.subTest(key=key, value=value):
                    self.assertFalse(p.supported({key: value, "name": "Bakery Cafe Restaurant"}))
        self.assertFalse(p.supported({"name": "Bakery"}))
        self.assertTrue(p.supported({"tourism": "hotel", "amenity": "restaurant"}))
        # Gas stations are not food venues, even when they carry shop=convenience.
        self.assertFalse(p.supported({"amenity": "fuel", "shop": "convenience"}))
        self.assertFalse(p.supported({"amenity": "fuel"}))
        self.assertTrue(p.supported({"shop": "convenience", "name": "Corner Store"}))

    def test_native_selectors_and_final_eligibility_have_identical_allowlists(self):
        self.assertEqual(len(p.SELECTORS), 2)
        parsed = {}
        for selector in p.SELECTORS:
            key, values = selector.removeprefix("nwr/").split("=")
            parsed[key] = values.split(",")
        self.assertEqual(parsed, {"amenity": list(p.AMENITIES), "shop": list(p.FOOD_SHOPS)})
        for key, values in parsed.items():
            for value in values:
                self.assertTrue(p.supported({key: value}))

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

    @staticmethod
    def transfer(data):
        def run(command, **kwargs):
            assert command[0] == "curl"
            assert "--location" not in command
            assert "--disable" == command[1]
            Path(command[command.index("--output") + 1]).write_bytes(data)
            return unittest.mock.Mock(stdout=json.dumps({"http_code": 200, "time_total": 1.5, "speed_download": 1234}))
        return run

    def test_checksum_and_source_provenance(self):
        data = b"test download bytes, not a real PBF"
        checksum = hashlib.md5(data).hexdigest().encode() + b"  source.osm.pbf\n"
        opener = unittest.mock.Mock()
        opener.open.return_value = self.reply(checksum)
        with tempfile.TemporaryDirectory() as folder, patch.object(p.urllib.request, "build_opener", return_value=opener), patch.object(p.subprocess, "run", side_effect=self.transfer(data)) as curl:
            work = Path(folder)
            p.download("washington", work)
            self.assertEqual((work / "source.osm.pbf").read_bytes(), data)
            source = p.load(work / "source.json")
            self.assertTrue(source["checksum_verified"])
            self.assertEqual(source["sha256"], hashlib.sha256(data).hexdigest())
            self.assertEqual(source["download_transport"], "curl")
            self.assertIn("--max-filesize", curl.call_args.args[0])

    def test_bad_checksum_never_promotes_partial_download(self):
        opener = unittest.mock.Mock()
        opener.open.side_effect = [self.reply(b"0" * 32 + b"  source\n") for _ in range(2)]
        with tempfile.TemporaryDirectory() as folder, patch.object(p.urllib.request, "build_opener", return_value=opener), patch.object(p.time, "sleep"), patch.object(p.subprocess, "run", side_effect=self.transfer(b"bad")):
            work = Path(folder)
            (work / "source.osm.pbf").write_bytes(b"previous good base")
            with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                p.download("washington", work)
            self.assertEqual((work / "source.osm.pbf").read_bytes(), b"previous good base")
            self.assertFalse((work / "source.partial").exists())

    def test_size_limit(self):
        opener = unittest.mock.Mock()
        opener.open.side_effect = [self.reply(b"0" * 32) for _ in range(2)]
        with tempfile.TemporaryDirectory() as folder, patch.object(p.urllib.request, "build_opener", return_value=opener), patch.object(p.time, "sleep"), patch.object(p.subprocess, "run", side_effect=self.transfer(b"large")):
            with self.assertRaisesRegex(ValueError, "size"):
                p.download("washington", Path(folder), max_bytes=2)

    def test_latest_redirect_is_validated_and_checksum_verified(self):
        data = b"redirected snapshot bytes"
        url = p.load(ROOT / "regions.json")["regions"]["washington"]
        dated = url.replace("latest", "261031")
        checksum = hashlib.md5(data).hexdigest().encode()
        opener = unittest.mock.Mock()
        opener.open.return_value = self.reply(checksum)
        calls = []
        final_transfer = self.transfer(data)
        def run(command, **kwargs):
            calls.append(command[-1])
            self.assertNotIn("--location", command)
            if len(calls) == 1:
                return unittest.mock.Mock(stdout=json.dumps({"http_code": 302, "redirect_url": dated, "time_total": .1}))
            return final_transfer(command, **kwargs)
        with tempfile.TemporaryDirectory() as folder, patch.object(p.urllib.request, "build_opener", return_value=opener), patch.object(p.subprocess, "run", side_effect=run):
            work = Path(folder)
            p.download("washington", work)
            self.assertEqual((work / "source.osm.pbf").read_bytes(), data)
            meta = p.load(work / "source.json")
            self.assertTrue(meta["checksum_verified"])
            self.assertEqual(meta["resolved_url"], dated)
            self.assertEqual(meta["validated_redirects"], 1)
            self.assertAlmostEqual(meta["transfer_seconds"], 1.6)
        self.assertEqual(calls, [url, dated])

    def test_unsafe_redirect_rejected_before_second_connection(self):
        url = "https://download.geofabrik.de/source.osm.pbf"
        for target in ("http://download.geofabrik.de/source.osm.pbf", "https://evil.invalid/file",
                       "https://download.geofabrik.de.evil.invalid/file", "file:///etc/passwd",
                       "https://download.geofabrik.de:444/file", "https://user:pass@download.geofabrik.de/file"):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as folder, patch.object(p.subprocess, "run") as run:
                run.return_value.stdout = json.dumps({"http_code": 302, "redirect_url": target})
                with self.assertRaisesRegex(ValueError, "Only HTTPS Geofabrik"):
                    p.curl_download(url, Path(folder) / "partial", 1000, time.monotonic() + 10)
                run.assert_called_once()

    def test_redirect_limit_and_missing_location(self):
        url = "https://download.geofabrik.de/source.osm.pbf"
        with tempfile.TemporaryDirectory() as folder, patch.object(p.subprocess, "run") as run:
            run.return_value.stdout = json.dumps({"http_code": 302, "redirect_url": url})
            with self.assertRaisesRegex(ValueError, "redirect limit"):
                p.curl_download(url, Path(folder) / "partial", 1000, time.monotonic() + 10)
            self.assertEqual(run.call_count, 6)  # One request + at most five hops.
            run.reset_mock()
            run.return_value.stdout = json.dumps({"http_code": 302, "redirect_url": ""})
            with self.assertRaisesRegex(ValueError, "Location"):
                p.curl_download(url, Path(folder) / "partial", 1000, time.monotonic() + 10)
            run.assert_called_once()

    def test_redirects_share_deadline_and_accept_relative_locations(self):
        url = "https://download.geofabrik.de/source-latest.osm.pbf"
        with tempfile.TemporaryDirectory() as folder, patch.object(p.subprocess, "run") as run, patch.object(p.time, "monotonic", side_effect=[100, 104]):
            run.side_effect = [unittest.mock.Mock(stdout=json.dumps({"http_code": 307, "redirect_url": "source-dated.osm.pbf"})),
                               unittest.mock.Mock(stdout=json.dumps({"http_code": 200}))]
            result = p.curl_download(url, Path(folder) / "partial", 1000, 110)
            self.assertEqual(result["resolved_url"], "https://download.geofabrik.de/source-dated.osm.pbf")
            self.assertEqual([call.args[0][call.args[0].index("--max-time") + 1] for call in run.call_args_list], ["10", "6"])
        with tempfile.TemporaryDirectory() as folder, patch.object(p.subprocess, "run") as run, patch.object(p.time, "monotonic", side_effect=[100, 111]):
            run.return_value.stdout = json.dumps({"http_code": 302, "redirect_url": url})
            with self.assertRaises(TimeoutError):
                p.curl_download(url, Path(folder) / "partial", 1000, 110)
            run.assert_called_once()

    @unittest.skipUnless(shutil.which("curl") and shutil.which("openssl"), "curl and openssl required")
    def test_real_curl_https_redirect_without_external_download(self):
        """Actual curl + TLS + 302/307 responses, not a mocked curl invocation."""
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            key, cert = root / "key.pem", root / "cert.pem"
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                            "-keyout", str(key), "-out", str(cert), "-subj", "/CN=127.0.0.1",
                            "-addext", "subjectAltName=IP:127.0.0.1"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
            requested = []
            data = b"tiny local TLS transport fixture, not geographic data"
            class Handler(http.server.BaseHTTPRequestHandler):
                def do_GET(self):
                    requested.append(self.path)
                    if self.path == "/latest":
                        self.send_response(302)
                        self.send_header("Location", "/dated")
                        body = b"redirect"
                    elif self.path == "/unsafe":
                        self.send_response(307)
                        self.send_header("Location", "http://127.0.0.1/forbidden")
                        body = b"unsafe redirect"
                    else:
                        self.send_response(200)
                        body = data
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                def log_message(self, *args):
                    pass
            server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(cert, key)
            server.socket = context.wrap_socket(server.socket, server_side=True)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            origin = "https://127.0.0.1:" + str(server.server_port)
            original_policy = p.check_url
            def policy(url):
                # Only substitute our ephemeral TLS origin; other URLs retain
                # the production policy. No external network is contacted.
                if not url.startswith(origin + "/"):
                    original_policy(url)
            try:
                with patch.object(p, "check_url", side_effect=policy), patch.dict(os.environ, {"CURL_CA_BUNDLE": str(cert), "NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1"}):
                    target = root / "source.partial"
                    result = p.curl_download(origin + "/latest", target, 1000, time.monotonic() + 10)
                    self.assertEqual(target.read_bytes(), data)
                    self.assertEqual(result["validated_redirects"], 1)
                    self.assertEqual(requested, ["/latest", "/dated"])
                    with self.assertRaises(ValueError):
                        p.curl_download(origin + "/unsafe", target, 1000, time.monotonic() + 10)
                    self.assertEqual(requested, ["/latest", "/dated", "/unsafe"])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


class SourceCache(unittest.TestCase):
    """Real tiny PBF headers and hashes; transports/native checker are isolated."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.work = self.root / "work"
        self.work.mkdir()
        self.url = p.load(ROOT / "regions.json")["regions"]["washington"]
        self.replication = self.url.removesuffix("-latest.osm.pbf") + "-updates"
        self.now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0) - dt.timedelta(hours=1)
        self.download = unittest.mock.Mock(side_effect=AssertionError("Unexpected full download"))
        self.native_run, self.native_refs = c.subprocess.run, c.check_way_nodes
        self.process = self.enterContext(patch.object(c.subprocess, "run", side_effect=AssertionError("Unexpected subprocess/network")))
        self.refs = self.enterContext(patch.object(c, "check_way_nodes"))
        self.enterContext(patch.object(p.urllib.request.OpenerDirector, "open", side_effect=AssertionError("Unexpected HTTP request")))

    def pbf(self, path, url=None, sequence=100, timestamp=None, empty_header=False, history=False):
        import osmium
        header = osmium.io.Header()
        header.has_multiple_object_versions = history
        if not empty_header:
            header.set("osmosis_replication_base_url", url or self.replication)
            header.set("osmosis_replication_sequence_number", str(sequence))
            header.set("osmosis_replication_timestamp", (timestamp or self.now).strftime("%Y-%m-%dT%H:%M:%SZ"))
        path.unlink(missing_ok=True)
        with osmium.SimpleWriter(osmium.io.File(str(path), "pbf,history=true" if history else "pbf"), header=header) as writer:
            writer.add_node(osmium.osm.mutable.Node(id=1, version=1, location=(-122.3, 47.6)))
        return path

    def seed(self, **kwargs):
        seed = self.pbf(self.root / "seed.osm.pbf", **kwargs)
        return c.prepare("washington", self.url, self.work, "seed", full_download=self.download,
                         seed_file=seed, seed_sha256=c.sha256(seed))

    def prepare(self, mode="reuse", allow=False):
        return c.prepare("washington", self.url, self.work, mode, allow, full_download=self.download)

    def snapshot(self, region, work):
        path = self.pbf(work / "source.osm.pbf")
        p.save(work / "source.json", {"region": region, "url": self.url, "bytes": path.stat().st_size,
               "sha256": c.sha256(path), "md5": hashlib.md5(path.read_bytes()).hexdigest(), "checksum_verified": True})

    def updater(self, exit_code=0, sequence=101, timestamp=None, output=True):
        def run(command, **kwargs):
            self.assertEqual(command[0], "pyosmium-up-to-date")
            self.assertEqual(command[command.index("--server") + 1], self.replication)
            self.assertIn("--socket-timeout", command)
            self.assertEqual(kwargs["timeout"], 1200)
            if output:
                self.pbf(Path(command[command.index("--outfile") + 1]), sequence=sequence,
                         timestamp=timestamp or self.now + dt.timedelta(minutes=1))
            return unittest.mock.Mock(returncode=exit_code)
        return run

    def test_default_auto_downloads_on_miss_then_reuses_without_requests(self):
        self.download.side_effect = self.snapshot
        cold = c.prepare("washington", self.url, self.work, full_download=self.download)
        self.assertTrue(cold["cache_changed"])
        self.assertTrue(cold["full_download_performed"])
        self.assertEqual(cold["mode"], "auto")
        self.assertEqual(cold["result"], "fresh_snapshot_downloaded")
        self.download.assert_called_once_with("washington", self.work)
        original = (self.work / "source.json").read_bytes()
        warm = c.prepare("washington", self.url, self.work, full_download=self.download)
        self.assertEqual(warm["result"], "cached_snapshot_reused")
        self.assertFalse(warm["cache_changed"])
        self.assertFalse(warm["full_download_performed"])
        self.assertEqual((self.work / "source.json").read_bytes(), original)
        self.download.assert_called_once()
        self.process.assert_not_called()

    def test_auto_invalid_cache_downloads_replacement(self):
        self.seed()
        (self.work / "source.osm.pbf").write_bytes(b"corrupt")
        self.download.side_effect = self.snapshot
        state = c.prepare("washington", self.url, self.work, full_download=self.download)
        self.assertTrue(state["full_download_performed"])
        self.assertFalse(state["cache_hit"])
        c.validate_checkpoint("washington", self.url, self.work)
        self.download.assert_called_once()

    def test_auto_download_permission_can_be_disabled(self):
        with self.assertRaisesRegex(c.SourceUnavailable, "Full downloads are disabled"):
            self.prepare("auto", allow=False)
        self.download.assert_not_called()
        self.process.assert_not_called()

    def test_cli_default_auto_downloads_once_and_opt_out_blocks_a_miss(self):
        output = self.root / "github-output.txt"
        argv = ["pipeline.py", "prepare-source", "--region", "washington", "--work", str(self.work)]
        with patch.object(sys, "argv", argv + ["--no-full-download"]), patch.object(p, "download", side_effect=self.snapshot) as download:
            with self.assertRaisesRegex(c.SourceUnavailable, "Full downloads are disabled"):
                p.main()
            download.assert_not_called()
        with patch.object(sys, "argv", argv), patch.dict(os.environ, {"GITHUB_OUTPUT": str(output)}), patch.object(p, "download", side_effect=self.snapshot) as download:
            self.assertEqual(p.main(), 0)
            self.assertEqual(output.read_text(), "cache_changed=true\ncache_ready=true\n")
            self.assertEqual(p.main(), 0)
            download.assert_called_once_with("washington", self.work)

    def test_reuse_missing_never_downloads_even_when_permission_is_true(self):
        for allow in (False, True):
            with self.assertRaisesRegex(c.SourceUnavailable, "Reuse mode never downloads"):
                self.prepare(allow=allow)
        self.download.assert_not_called()
        self.process.assert_not_called()
        self.assertFalse((self.work / "cache-ready.json").exists())
        self.assertFalse(p.load(self.work / "cache-state.json")["full_download_performed"])

    def test_seed_then_reuse_is_offline_and_does_not_resave(self):
        state = self.seed()
        self.assertTrue(state["cache_changed"])
        original = (self.work / "source.json").read_bytes()
        self.refs.reset_mock()
        state = self.prepare(allow=True)
        self.assertTrue(state["cache_hit"])
        self.assertFalse(state["cache_changed"])
        self.assertEqual(state["result"], "cached_snapshot_reused")
        self.assertEqual((self.work / "source.json").read_bytes(), original)
        self.refs.assert_not_called()
        self.download.assert_not_called()
        self.process.assert_not_called()

    def test_corrupt_and_wrong_region_caches_fail_closed(self):
        mutations = [lambda meta: meta.update(region="california"),
                     lambda meta: meta.update(url=self.url.replace("washington", "california")),
                     lambda meta: meta.update(sha256="0" * 64),
                     lambda meta: meta.update(bytes=1),
                     lambda meta: meta.update(checkpoint_schema=0),
                     lambda meta: meta.update(replication={}),
                     lambda meta: meta.pop("way_nodes_verified")]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                self.seed()
                meta = p.load(self.work / "source.json")
                mutate(meta)
                p.save(self.work / "source.json", meta)
                with self.assertRaises(c.SourceUnavailable):
                    self.prepare()
        self.seed()
        (self.work / "source.osm.pbf").write_bytes(b"corrupt")
        with self.assertRaises(c.SourceUnavailable):
            self.prepare()
        self.download.assert_not_called()
        self.process.assert_not_called()

    def test_fresh_requires_explicit_permission(self):
        with self.assertRaisesRegex(c.SourceUnavailable, "Full downloads are disabled"):
            self.prepare("fresh")
        self.download.assert_not_called()
        self.download.side_effect = self.snapshot
        result = self.prepare("fresh", allow=True)
        self.assertTrue(result["cache_changed"])
        self.assertTrue(result["full_download_performed"])
        meta, _ = c.validate_checkpoint("washington", self.url, self.work)
        self.assertEqual(meta["verification"], "snapshot_md5")
        self.assertTrue(meta["checksum_verified"])

    def test_update_miss_only_downloads_when_explicitly_allowed(self):
        with self.assertRaisesRegex(c.SourceUnavailable, "Full downloads are disabled"):
            self.prepare("update")
        self.download.assert_not_called()
        self.download.side_effect = self.snapshot
        self.assertTrue(self.prepare("update", allow=True)["full_download_performed"])

    def test_update_no_changes_can_have_no_outfile(self):
        self.seed()
        self.process.side_effect = self.updater(output=False)
        state = self.prepare("update")
        self.assertEqual(state["result"], "no_new_diffs")
        self.assertFalse(state["cache_changed"])
        self.download.assert_not_called()

    def test_atomic_replication_records_new_bytes_not_original_md5(self):
        self.download.side_effect = self.snapshot
        self.prepare("fresh", allow=True)
        before = p.load(self.work / "source.json")
        self.download.reset_mock()
        self.process.side_effect = self.updater()
        state = self.prepare("update")
        self.assertEqual(state["result"], "replication_updated")
        after, header = c.validate_checkpoint("washington", self.url, self.work)
        self.assertEqual(header["sequence"], 101)
        self.assertFalse(after["checksum_verified"])
        self.assertNotIn("md5", after)
        self.assertEqual(after["verified_snapshot_md5"], before["md5"])
        self.assertEqual(after["previous_checkpoint_sha256"], before["sha256"])
        self.assertEqual(after["verification"], "replication_from_verified_checkpoint")
        self.download.assert_not_called()

    def test_partial_failed_and_backward_updates_preserve_original(self):
        for updater in (self.updater(exit_code=1), self.updater(exit_code=2),
                        self.updater(sequence=99), self.updater(timestamp=self.now - dt.timedelta(minutes=1))):
            with self.subTest(updater=updater):
                self.seed()
                before = (self.work / "source.osm.pbf").read_bytes()
                metadata = (self.work / "source.json").read_bytes()
                self.process.side_effect = updater
                with self.assertRaises(c.SourceUnavailable):
                    self.prepare("update")
                self.assertEqual((self.work / "source.osm.pbf").read_bytes(), before)
                self.assertEqual((self.work / "source.json").read_bytes(), metadata)
                self.assertFalse((self.work / "updated.osm.pbf").exists())
                self.assertFalse((self.work / "cache-ready.json").exists())
        self.download.assert_not_called()

    def test_update_timeout_and_bad_references_preserve_original(self):
        self.seed()
        original = c.sha256(self.work / "source.osm.pbf")
        self.process.side_effect = subprocess.TimeoutExpired("updater", 1200)
        with self.assertRaises(c.SourceUnavailable):
            self.prepare("update")
        self.assertEqual(c.sha256(self.work / "source.osm.pbf"), original)
        self.process.side_effect = self.updater()
        self.refs.side_effect = subprocess.CalledProcessError(1, "osmium check-refs")
        with self.assertRaises(c.SourceUnavailable):
            self.prepare("update")
        self.assertEqual(c.sha256(self.work / "source.osm.pbf"), original)
        self.download.assert_not_called()

    def test_explicit_update_fallback_is_reported(self):
        self.seed()
        self.process.side_effect = self.updater(exit_code=1)
        self.download.side_effect = self.snapshot
        state = self.prepare("update", allow=True)
        self.assertTrue(state["full_download_performed"])
        self.assertEqual(state["result"], "fresh_snapshot_downloaded")

    def test_old_and_headerless_seeds_reusable_but_not_incrementally_updated(self):
        for kwargs in ({"timestamp": self.now - dt.timedelta(days=91)}, {"empty_header": True}):
            with self.subTest(kwargs=kwargs):
                self.seed(**kwargs)
                self.assertEqual(self.prepare()["result"], "cached_snapshot_reused")
                with self.assertRaises(c.SourceUnavailable):
                    self.prepare("update")
        self.process.assert_not_called()
        self.download.assert_not_called()

    def test_legacy_http_header_normalization_keeps_sequence_and_time(self):
        self.seed(url=self.replication.replace("https:", "http:") + "/")
        def run(command, **kwargs):
            if command[0] == "osmium":
                self.assertIn("--output-header=osmosis_replication_timestamp!", command)
                self.assertIn("--output-header=osmosis_replication_sequence_number!", command)
                self.pbf(Path(command[command.index("-o") + 1]))
                return unittest.mock.Mock(returncode=0)
            return self.updater(output=False)(command, **kwargs)
        self.process.side_effect = run
        self.assertTrue(self.prepare("update")["cache_changed"])
        _, header = c.validate_checkpoint("washington", self.url, self.work)
        self.assertEqual(header["url"], self.replication)
        self.assertEqual(header["sequence"], 100)
        self.assertEqual(header["timestamp"], self.now.isoformat())
        self.assertFalse((self.work / "normalized.osm.pbf").exists())

    def test_wrong_seed_checksum_region_history_and_untrusted_servers_rejected(self):
        bad_seed = self.pbf(self.root / "bad.osm.pbf")
        with self.assertRaises(c.SourceUnavailable):
            c.prepare("washington", self.url, self.work, "seed", seed_file=bad_seed, seed_sha256="0" * 64)
        for kwargs in ({"url": self.replication.replace("washington", "california")},
                       {"url": "https://planet.osm.org/replication/hour/"}, {"history": True}):
            with self.subTest(kwargs=kwargs), self.assertRaises(c.SourceUnavailable):
                self.seed(**kwargs)
        self.process.assert_not_called()
        self.download.assert_not_called()

    def test_update_disk_reserve_blocks_before_network(self):
        self.seed()
        with patch.object(c.shutil, "disk_usage", return_value=unittest.mock.Mock(free=1)):
            with self.assertRaisesRegex(c.SourceUnavailable, "disk reserve"):
                self.prepare("update")
        self.process.assert_not_called()
        self.download.assert_not_called()

    @unittest.skipUnless(shutil.which("osmium"), "native osmium-tool unavailable")
    def test_native_header_normalization_and_seed_reference_check(self):
        self.refs.side_effect = self.native_refs
        def run(command, **kwargs):
            if command[0] == "osmium":
                return self.native_run(command, **kwargs)
            return self.updater(output=False)(command, **kwargs)
        self.process.side_effect = run
        self.seed(url=self.replication.replace("https:", "http:") + "/")
        state = self.prepare("update")
        self.assertTrue(state["cache_changed"])
        _, header = c.validate_checkpoint("washington", self.url, self.work)
        self.assertEqual(header["url"], self.replication)
        self.assertEqual(header["sequence"], 100)
        self.assertEqual(header["timestamp"], self.now.isoformat())
        self.download.assert_not_called()

    def test_prepare_cli_defaults_to_auto_and_reports_cached_replay(self):
        self.seed()
        output = self.root / "github-output.txt"
        with patch.object(sys, "argv", ["pipeline.py", "prepare-source", "--region", "washington", "--work", str(self.work)]), patch.dict(os.environ, {"GITHUB_OUTPUT": str(output)}):
            self.assertEqual(p.main(), 0)
        self.assertEqual(output.read_text(), "cache_changed=false\ncache_ready=true\n")
        summary = self.root / "summary.json"
        p.report(self.work, summary)
        self.assertEqual(p.load(summary)["cache-state"]["result"], "cached_snapshot_reused")
        self.download.assert_not_called()
        self.process.assert_not_called()

    def test_workflow_default_and_checkpoint_order(self):
        workflow = (ROOT / ".github/workflows/benchmark.yml").read_text()
        self.assertRegex(workflow, r"source_mode:[\s\S]*?default: auto")
        self.assertRegex(workflow, r"allow_full_download:[\s\S]*?default: true")
        save = workflow.index("actions/cache/save@")
        self.assertLess(save, workflow.index("Native tag filter"))
        self.assertLess(workflow.index("Confirm the checkpoint is persisted"), workflow.index("Native tag filter"))
        self.assertLess(workflow.index("Fail closed on a cache miss"), workflow.index("Install native filter"))
        self.assertIn("steps.source.outputs.cache_changed == 'true'", workflow)
        self.assertNotIn("outputs.cache-hit", workflow)
        self.assertIn("work/cache-state.json", workflow)
        self.assertIn("else extra+=(--no-full-download)", workflow)


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

    def food_scope_source(self):
        import xml.etree.ElementTree as ET
        tree = ET.parse(ROOT / "tests/fixtures/places.osm")
        osm = tree.getroot()
        # Native filtering must keep this bank-tagged node as geometry support
        # for the restaurant polygon, but it must not become a search listing.
        dependency = osm.find("node[@id='100']")
        ET.SubElement(dependency, "tag", k="amenity", v="bank")
        ET.SubElement(dependency, "tag", k="name", v="Dependency Bank")
        for number, tags in enumerate((
            {"shop": "bakery"}, {"shop": "convenience"}, {"shop": "greengrocer"},
            {"amenity": "bank"}, {"amenity": "school"}, {"tourism": "camp_site"},
            {"shop": "clothes"}, {"leisure": "park"}, {"shop": "bakery", "closed": "yes"},
        ), 9000):
            node = ET.SubElement(osm, "node", id=str(number), version="1", lon="-122.35", lat="47.61")
            for key, value in {"name": "Synthetic Scope " + str(number), **tags}.items():
                ET.SubElement(node, "tag", k=key, v=value)
        bank = ET.SubElement(osm, "way", id="9000", version="1")
        for ref in (200, 201, 202, 203, 200):
            ET.SubElement(bank, "nd", ref=str(ref))
        ET.SubElement(bank, "tag", k="amenity", v="bank")
        ET.SubElement(bank, "tag", k="name", v="Bank Polygon")
        school = ET.SubElement(osm, "relation", id="9001", version="1")
        ET.SubElement(school, "member", type="way", ref="200", role="outer")
        ET.SubElement(school, "member", type="way", ref="201", role="inner")
        for key, value in {"name": "School Polygon", "amenity": "school", "type": "multipolygon"}.items():
            ET.SubElement(school, "tag", k=key, v=value)
        # A non-food polygon with missing geometry must not block a food build.
        relation = ET.SubElement(osm, "relation", id="9000", version="1")
        ET.SubElement(relation, "member", type="way", ref="999999", role="outer")
        for key, value in {"name": "School", "amenity": "school", "type": "multipolygon"}.items():
            ET.SubElement(relation, "tag", k=key, v=value)
        osm[:] = sorted(osm, key=lambda e: (("node", "way", "relation").index(e.tag), int(e.attrib["id"])))
        source = self.work / "food-scope.osm"
        tree.write(source, encoding="utf-8", xml_declaration=True)
        return source

    def test_food_scope_removes_nonfood_before_geometry_aliases_and_index(self):
        p.export_records(self.food_scope_source(), self.db)
        records = self.records()
        self.assertEqual(set(records), {"node:1", "node:3", "node:5", "node:6", "way:1",
                                       "relation:700", "node:9000", "node:9001", "node:9002"})
        self.assertEqual(p.load(self.work / "geometry-report.json")["rejected_count"], 0)
        self.assertEqual(p.load(self.work / "export-stats.json")["selection_profile"], "food-v2")
        with sqlite3.connect(self.db) as db:
            aliases = {row[0] for row in db.execute("SELECT DISTINCT id FROM names")}
            self.assertEqual(aliases, set(records))
        self.index()
        manifest = p.load(self.output / "manifest.json")
        self.assertEqual(manifest["selection"], {"profile": "food-v2", "amenity": list(p.AMENITIES), "shop": list(p.FOOD_SHOPS)})
        self.assertEqual(p.verify(self.output)["features"], 9)

    def test_old_broad_sqlite_cannot_be_relabeled_as_food_only(self):
        self.seed()
        with sqlite3.connect(self.db) as db:
            row = json.loads(db.execute("SELECT data FROM features WHERE id='node:1'").fetchone()[0])
            row["tags"] = {"amenity": "bank"}
            db.execute("UPDATE features SET data=? WHERE id='node:1'", (p.encoded(row).decode(),))
        with self.assertRaisesRegex(ValueError, "Non-food record"):
            self.index()

    @unittest.skipUnless(shutil.which("osmium"), "native osmium-tool is installed by hosted CI")
    def test_native_food_filter_retains_geometry_dependency_not_its_bank_listing(self):
        import osmium
        target = self.work / "food-scope.osm.pbf"
        subprocess.run(["osmium", "tags-filter", str(self.food_scope_source()), *p.SELECTORS, "-o", str(target)], check=True)
        class Dependencies(osmium.SimpleHandler):
            bank_present = False
            def node(handler, node):
                if node.id == 100:
                    handler.bank_present = node.tags.get("amenity") == "bank"
        dependencies = Dependencies()
        dependencies.apply_file(str(target))
        self.assertTrue(dependencies.bank_present)
        subprocess.run(["osmium", "check-refs", "-r", str(target)], check=True)
        p.export_records(target, self.db)
        self.assertEqual(set(self.records()), {"node:1", "node:3", "node:5", "node:6", "way:1",
                                              "relation:700", "node:9000", "node:9001", "node:9002"})

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


class GeometryQuality(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.source = self.work / "source.osm"
        self.source.write_text((ROOT / "tests/fixtures/places.osm").read_text().replace('relation id="700"', 'relation id="1330059"'))
        self.db = self.work / "records.sqlite"
        self.output = self.work / "output"

    def fail_factory(self, targets, message="invalid area (area_id=2660119)"):
        import osmium
        real = osmium.geom.WKBFactory()
        class Factory:
            def create_multipolygon(self, area):
                key = ("way" if area.from_way() else "relation", int(area.orig_id()))
                if key in targets:
                    raise RuntimeError(message)
                return real.create_multipolygon(area)
        return patch.object(osmium.geom, "WKBFactory", return_value=Factory())

    def index(self):
        p.save(self.work / "source.json", {"fixture": True})
        p.save(self.work / "header.json", {"fixture": True})
        p.build_indexes(self.db, self.output, self.work / "source.json", self.work / "header.json", "fixture")

    def test_strict_invalid_area_persists_typed_diagnostic(self):
        # Inject the reported library error at the WKB boundary while parsing
        # real fixture areas; this is not the actual production OSM geometry.
        with self.fail_factory({("relation", 1330059)}), self.assertRaises(p.InvalidGeometry):
            p.export_records(self.source, self.db)
        report = p.load(self.work / "geometry-report.json")
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["rejected_count"], 1)
        self.assertEqual(report["rejected_features"][0]["id"], "relation:1330059")
        self.assertEqual(report["rejected_features"][0]["area_id"], 2660119)

    def test_benchmark_continues_but_partial_dataset_cannot_be_promoted(self):
        with self.fail_factory({("relation", 1330059)}):
            p.export_records(self.source, self.db, quarantine_invalid_geometry=True)
        self.index()
        stats = p.verify(self.output)
        self.assertEqual(stats["features"], 5)
        self.assertFalse(stats["geometry_complete"])
        self.assertEqual(stats["geometry_rejected_count"], 1)
        with self.assertRaisesRegex(ValueError, "Public release blocked"):
            p.require_complete_geometry(self.output)
        p.package(self.output, self.work / "fixture.tar.gz")
        self.assertFalse(p.load(self.work / "fixture.tar.gz.json")["geometry_complete"])
        with tarfile.open(self.work / "fixture.tar.gz") as archive:
            self.assertIn("geometry-report.json", archive.getnames())

    def test_rejected_closed_way_cannot_keep_its_earlier_polygon(self):
        with self.fail_factory({("way", 1)}):
            p.export_records(self.source, self.db, quarantine_invalid_geometry=True)
        with sqlite3.connect(self.db) as db:
            self.assertIsNone(db.execute("SELECT id FROM features WHERE id='way:1'").fetchone())
            self.assertIsNone(db.execute("SELECT id FROM names WHERE id='way:1'").fetchone())
        self.assertEqual(p.load(self.work / "geometry-report.json")["rejected_features"][0]["id"], "way:1")

    def test_unrelated_runtime_errors_are_not_swallowed(self):
        with self.fail_factory({("relation", 1330059)}, "backend unavailable"), self.assertRaisesRegex(RuntimeError, "backend unavailable"):
            p.export_records(self.source, self.db, quarantine_invalid_geometry=True)
        report = p.load(self.work / "geometry-report.json")
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["rejected_count"], 0)

    def test_repair_never_treats_a_collapsed_polygon_as_an_area(self):
        from shapely import make_valid
        from shapely.geometry import Polygon
        collapsed = Polygon([(0,0), (1,0), (2,0), (0,0)])
        with self.assertRaisesRegex(p.InvalidGeometry, "collapsed"):
            p.representative_geometry(collapsed, True)
        bowtie = Polygon([(0,0), (2,2), (0,2), (2,0), (0,0)])
        point, repaired = p.representative_geometry(bowtie, True)
        self.assertTrue(repaired)
        self.assertTrue(make_valid(bowtie).covers(point))

    def test_complete_references_but_unassembled_area_is_reported(self):
        self.source.write_text(self.source.read_text().replace('</osm>', '<relation id="1330060" version="1"><member type="node" ref="1" role="label"/><tag k="type" v="multipolygon"/><tag k="amenity" v="restaurant"/><tag k="name" v="No rings"/></relation></osm>'))
        p.export_records(self.source, self.db, quarantine_invalid_geometry=True)
        report = p.load(self.work / "geometry-report.json")
        self.assertEqual(report["rejected_count"], 1)
        self.assertEqual(report["rejected_features"][0]["reason"], "unassembled_relation")

    def test_rejection_budget_stops_mass_data_loss(self):
        with self.fail_factory({("way", 1), ("relation", 1330059)}), self.assertRaisesRegex(p.InvalidGeometry, "limit exceeded"):
            p.export_records(self.source, self.db, quarantine_invalid_geometry=True, max_geometry_rejections=1)
        self.assertEqual(p.load(self.work / "geometry-report.json")["status"], "failed")

    def test_inconsistent_quality_report_is_rejected(self):
        p.export_records(self.source, self.db)
        self.index()
        p.require_complete_geometry(self.output)
        report = p.load(self.output / "geometry-report.json")
        report["rejected_count"] = 1
        report["rejected_features"] = [{"id": "node:1"}]
        p.save(self.output / "geometry-report.json", report)
        with self.assertRaisesRegex(ValueError, "Geometry quality"):
            p.verify(self.output)


class RelationAudit(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)

    def source(self, relation):
        source = self.work / "partial.osm"
        source.write_text((ROOT / "tests/fixtures/places.osm").read_text().replace("</osm>", relation + "</osm>"))
        return source

    def test_missing_unsupported_site_members_are_diagnostic_not_fatal(self):
        source = self.source('<relation id="800" version="1"><member type="way" ref="99999" role=""/><tag k="type" v="site"/><tag k="amenity" v="restaurant"/><tag k="name" v="Partial site"/></relation>')
        report = p.audit_relations(source, self.work / "audit.json")
        self.assertEqual(report["missing_unique_members"], {"nodes": 0, "ways": 1, "relations": 0})
        self.assertEqual(report["blocked_polygon_relation_ids"], [])
        self.assertEqual(report["affected_relations"][0]["id"], 800)
        p.export_records(source, self.work / "records.sqlite", relation_audit=self.work / "audit.json")
        stats = p.load(self.work / "export-stats.json")
        self.assertEqual(stats["features"], 6)
        self.assertEqual(stats["relation_integrity"]["affected_relation_count"], 1)

    def test_missing_outer_ring_blocks_even_if_another_outer_ring_is_present(self):
        source = self.source('<relation id="800" version="1"><member type="way" ref="200" role="outer"/><member type="way" ref="99999" role="outer"/><tag k="type" v="multipolygon"/><tag k="amenity" v="restaurant"/><tag k="name" v="Partial polygon"/></relation>')
        report = p.audit_relations(source, self.work / "audit.json")
        self.assertEqual(report["blocked_polygon_relation_ids"], [800])
        with self.assertRaisesRegex(ValueError, "incomplete selected relation"):
            p.export_records(source, self.work / "records.sqlite")
        self.assertEqual(p.load(self.work / "export-stats.json")["error"], "incomplete_selected_polygon")

    def test_nested_relation_gap_propagates_to_polygon_parent(self):
        source = self.source('<relation id="800" version="1"><member type="way" ref="99999" role=""/><tag k="type" v="site"/></relation><relation id="801" version="1"><member type="relation" ref="800" role="outer"/><tag k="type" v="multipolygon"/><tag k="amenity" v="restaurant"/><tag k="name" v="Nested partial"/></relation>')
        report = p.audit_relations(source, self.work / "audit.json")
        self.assertEqual(report["affected_relation_count"], 2)
        self.assertEqual(report["blocked_polygon_relation_ids"], [801])

    def test_stale_audit_is_rejected(self):
        source = self.source("")
        p.audit_relations(source, self.work / "audit.json")
        source.write_text(source.read_text().replace('version="3"', 'version="4"'))
        with self.assertRaisesRegex(ValueError, "does not match"):
            p.export_records(source, self.work / "records.sqlite", relation_audit=self.work / "audit.json")

    def test_missing_way_node_remains_fatal(self):
        import osmium
        source = self.source('<way id="800" version="1"><nd ref="1"/><nd ref="99999"/><tag k="amenity" v="restaurant"/><tag k="name" v="Broken way"/></way>')
        with self.assertRaises(osmium.InvalidLocationError):
            p.export_records(source, self.work / "records.sqlite")


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


class TilesAndMerge(unittest.TestCase):
    """Tile planning, gas-station exclusion and typed-ID merge of regional/tile exports."""

    BOX = (-122.46, 47.48, -122.20, 47.75)
    FULL_IDS = {"node:1", "node:3", "node:5", "way:1", "relation:700"}

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)

    def make_input(self, name, bbox=None, source=None):
        directory = self.work / name
        directory.mkdir()
        p.export_records(source or ROOT / "tests/fixtures/places.osm", directory / "records.sqlite", bbox,
                         as_of=dt.date(2026, 11, 5))
        p.save(directory / "source.json", {"region": "fixture", "sha256": "fixture", "checksum_verified": True})
        p.save(directory / "pbf-header.json", {"fixture": True})
        return directory

    def rows(self, dbpath):
        with sqlite3.connect(dbpath) as db:
            return {row[0]: row[1] for row in db.execute("SELECT id,data FROM features")}

    def test_tiles_share_edges_and_cover_the_box(self):
        boxes = dict(p.tile_boxes([-74.5, 40.35, -73.35, 41.35], 2, 2))
        self.assertEqual(sorted(boxes), [0, 1, 2, 3])
        self.assertEqual(boxes[0], [-74.5, 40.35, -73.925, 40.85])
        self.assertEqual(boxes[3], [-73.925, 40.85, -73.35, 41.35])
        # Neighbouring tiles share exact edges; the inclusive filter then emits
        # boundary objects twice, which the merge removes by typed ID.
        self.assertEqual(boxes[0][2], boxes[2][0])
        self.assertEqual(boxes[0][3], boxes[1][1])

    def test_plan_tile_jobs_share_a_group_and_have_unique_keys(self):
        jobs = p.plan("new-york", "ignored")["include"]
        self.assertEqual({job["group"] for job in jobs}, {"new-york"})
        self.assertEqual(len({job["key"] for job in jobs}), 8)
        self.assertEqual({job["region"] for job in jobs}, {"new-york", "new-jersey"})
        self.assertEqual({job["bbox"].split(",")[0] for job in jobs}, {"-74.5", "-73.925"})
        seattle = p.plan("seattle", "ignored")["include"]
        self.assertEqual([job["group"] for job in seattle], ["seattle"])
        full = p.plan("full-regions", "washington,california")["include"]
        self.assertEqual([job["group"] for job in full], ["washington-full", "california-full"])

    def test_gas_station_is_removed_but_a_separate_shop_survives(self):
        import xml.etree.ElementTree as ET
        tree = ET.parse(ROOT / "tests/fixtures/places.osm")
        osm = tree.getroot()
        for ident, tags in ((9100, {"amenity": "fuel", "shop": "convenience"}), (9101, {"amenity": "fuel"}),
                            (9102, {"shop": "convenience"})):
            node = ET.SubElement(osm, "node", id=str(ident), version="1", lon="-122.35", lat="47.61")
            for key, value in {"name": "Synthetic " + str(ident), **tags}.items():
                ET.SubElement(node, "tag", k=key, v=value)
        source = self.work / "gas.osm"
        tree.write(source, encoding="utf-8", xml_declaration=True)
        dbpath = self.work / "gas.sqlite"
        p.export_records(source, dbpath, self.BOX, as_of=dt.date(2026, 11, 5))
        records = self.rows(dbpath)
        self.assertNotIn("node:9100", records)
        self.assertNotIn("node:9101", records)
        self.assertIn("node:9102", records)

    def test_fuel_station_cannot_be_smuggled_into_an_old_database(self):
        dbpath = self.work / "old.sqlite"
        p.export_records(ROOT / "tests/fixtures/places.osm", dbpath, self.BOX, as_of=dt.date(2026, 11, 5))
        record = {"id": "node:9100", "name": "Gas", "osm_version": 1, "point": [-122.35, 47.61],
                  "tags": {"amenity": "fuel", "shop": "convenience"}, "geometry": "node",
                  "verification": "source_tags_only"}
        with sqlite3.connect(dbpath) as db:
            db.execute("INSERT INTO features VALUES (?,?,?,?,NULL)",
                       ("node:9100", "12/0/0", "8/0/0", p.encoded(record).decode()))
        p.save(self.work / "source.json", {"sha256": "fixture"})
        p.save(self.work / "header.json", {"fixture": True})
        with self.assertRaisesRegex(ValueError, "Non-food record"):
            p.build_indexes(dbpath, self.work / "out", self.work / "source.json", self.work / "header.json", "unit-test")

    def test_overlapping_tiles_merge_to_the_whole_box_export(self):
        whole = self.make_input("whole", self.BOX)
        left = self.make_input("left", (-122.46, 47.48, -122.35, 47.75))
        right = self.make_input("right", (-122.35, 47.48, -122.20, 47.75))
        # Both tiles contain every object at lon=-122.35; the duplicate must collapse.
        self.assertTrue({"node:1", "node:3", "node:5"} <= set(self.rows(left / "records.sqlite")))
        self.assertTrue({"node:1", "node:3", "node:5"} <= set(self.rows(right / "records.sqlite")))
        merged = self.work / "merged"
        result = p.merge_records([right, left], merged)
        self.assertEqual(result, {"features": 5, "rejected": 0})
        self.assertEqual(self.rows(merged / "records.sqlite"), self.rows(whole / "records.sqlite"))
        stats = p.load(merged / "export-stats.json")
        self.assertEqual(stats["selection_counts"], {"amenity=cafe": 1, "amenity=restaurant": 3, "shop=supermarket": 1})
        self.assertEqual(len(stats["merged_inputs"]), 2)

    def test_newest_osm_version_wins_and_order_does_not_matter(self):
        old = self.make_input("old", self.BOX)
        new = self.make_input("new", self.BOX)
        with sqlite3.connect(new / "records.sqlite") as db:
            record = json.loads(db.execute("SELECT data FROM features WHERE id='node:1'").fetchone()[0])
            record["osm_version"], record["name"] = 9, "Edited Name"
            db.execute("UPDATE features SET data=? WHERE id='node:1'", (p.encoded(record).decode(),))
        for order in ([old, new], [new, old]):
            merged = self.work / ("merged-" + "-".join(d.name for d in order))
            p.merge_records(order, merged)
            with sqlite3.connect(merged / "records.sqlite") as db:
                row = json.loads(db.execute("SELECT data FROM features WHERE id='node:1'").fetchone()[0])
                self.assertEqual((row["osm_version"], row["name"]), (9, "Edited Name"))
                self.assertEqual(db.execute("SELECT count(*) FROM features").fetchone()[0], 5)

    def test_quarantine_in_any_input_removes_the_feature_and_blocks_release(self):
        whole = self.make_input("whole", self.BOX)
        quarantined = self.make_input("quarantined", self.BOX)
        with sqlite3.connect(quarantined / "records.sqlite") as db:
            db.execute("DELETE FROM features WHERE id='way:1'")
            db.execute("INSERT INTO geometry_rejections VALUES (?,?)", ("way:1", p.encoded(
                {"id": "way:1", "name": "Concave Test", "reason": "invalid_polygon_geometry", "message": "synthetic"}).decode()))
        report = p.load(quarantined / "geometry-report.json")
        report.update(rejected_count=1, rejected_features=[{"id": "way:1", "name": "Concave Test", "reason": "invalid_polygon_geometry", "message": "synthetic"}], geometry_quality="quarantined")
        p.save(quarantined / "geometry-report.json", report)
        stats = p.load(quarantined / "export-stats.json")
        stats.update(geometry_rejected_count=1, geometry_quality="quarantined")
        p.save(quarantined / "export-stats.json", stats)
        merged = self.work / "merged"
        self.assertEqual(p.merge_records([whole, quarantined], merged), {"features": 4, "rejected": 1})
        self.assertNotIn("way:1", self.rows(merged / "records.sqlite"))
        self.assertEqual(p.load(merged / "geometry-report.json")["geometry_quality"], "quarantined")
        output = self.work / "output"
        p.build_indexes(merged / "records.sqlite", output, merged / "source.json", merged / "pbf-header.json", "unit-test")
        self.assertEqual(p.verify(output)["geometry_rejected_count"], 1)
        with self.assertRaisesRegex(ValueError, "quarantined"):
            p.require_complete_geometry(output)

    def test_merged_bundle_verifies_and_matches_the_single_box_bundle(self):
        whole = self.make_input("whole", self.BOX)
        left = self.make_input("left", (-122.46, 47.48, -122.35, 47.75))
        right = self.make_input("right", (-122.35, 47.48, -122.20, 47.75))
        merged = self.work / "merged"
        p.merge_records([left, right], merged)
        output = self.work / "output"
        p.build_indexes(merged / "records.sqlite", output, merged / "source.json", merged / "pbf-header.json", "benchmark")
        stats = p.verify(output)
        self.assertEqual(stats["features"], 5)
        self.assertEqual(stats["geometry_rejected_count"], 0)
        reference = self.work / "reference"
        p.build_indexes(whole / "records.sqlite", reference, whole / "source.json", whole / "pbf-header.json", "benchmark")
        self.assertEqual(p.verify(reference)["features"], 5)
        self.assertEqual(p.load(output / "manifest.json")["feature_count"], p.load(reference / "manifest.json")["feature_count"])

    def test_merge_refuses_mixed_closure_dates_profiles_and_failed_inputs(self):
        first = self.make_input("first", self.BOX)
        other_day = self.make_input("other-day", self.BOX)
        stats = p.load(other_day / "export-stats.json")
        stats["closure_as_of"] = "2026-11-06"
        p.save(other_day / "export-stats.json", stats)
        with self.assertRaisesRegex(ValueError, "closure dates"):
            p.merge_records([first, other_day], self.work / "m1")
        old_profile = self.make_input("old-profile", self.BOX)
        stats = p.load(old_profile / "export-stats.json")
        stats["selection_profile"] = "food-v1"
        p.save(old_profile / "export-stats.json", stats)
        with self.assertRaisesRegex(ValueError, "current-profile"):
            p.merge_records([first, old_profile], self.work / "m2")
        failed = self.make_input("failed", self.BOX)
        stats = p.load(failed / "export-stats.json")
        stats["export_status"] = "failed"
        p.save(failed / "export-stats.json", stats)
        with self.assertRaisesRegex(ValueError, "current-profile"):
            p.merge_records([first, failed], self.work / "m3")
        with self.assertRaisesRegex(ValueError, "distinct"):
            p.merge_records([first, first], self.work / "m4")

    def test_workflow_tile_jobs_use_one_checkpoint_and_one_closure_date(self):
        import yaml
        workflow = yaml.load((ROOT / ".github/workflows/benchmark.yml").read_text(), Loader=yaml.BaseLoader)
        jobs = workflow["jobs"]
        self.assertEqual(jobs["build"]["needs"], ["plan", "source"])
        self.assertEqual(jobs["merge"]["needs"], ["plan", "build"])
        # Ordinary runs skip prepare-release; build and merge must not inherit that skip.
        self.assertIn("needs.source.result == 'success'", jobs["build"]["if"])
        self.assertIn("needs.build.result == 'success'", jobs["merge"]["if"])
        self.assertEqual(jobs["publish"]["needs"], ["plan", "merge"])
        self.assertIn("--as-of", " ".join(step.get("run", "") for step in jobs["build"]["steps"]))
        build_uses = [step.get("uses", "") for step in jobs["build"]["steps"]]
        self.assertFalse(any("actions/cache/save" in use for use in build_uses))
        self.assertTrue(any("actions/cache/restore" in use for use in build_uses))
        self.assertIn("records-${{ matrix.group }}--*", str(jobs["merge"]["steps"]))
        self.assertIn("as_of", jobs["plan"]["outputs"])

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
                    self.assertRegex(step["uses"], r"^actions/[a-z-]+(?:/[a-z-]+)?@[a-f0-9]{40}$")
        self.assertNotIn("secrets.", path.read_text())
        self.assertNotIn("curl ", path.read_text())
        self.assertIn("osmium check-refs work/filtered.osm.pbf", path.read_text())
        self.assertNotIn("osmium check-refs -r work/filtered.osm.pbf", path.read_text())
        self.assertIn("--relation-audit work/relation-audit.json", path.read_text())
        self.assertIn("--quarantine-invalid-geometry", path.read_text())
        self.assertIn("require-complete-geometry --output output", path.read_text())
        self.assertIn("work/geometry-report.json", path.read_text())
        text = path.read_text()
        self.assertLess(text.index("require-complete-geometry --output output"),
                        text.index('gh release upload "$TAG" "artifacts/${GROUP}.tar.gz"'))
        self.assertIn("work/relation-audit.json", path.read_text())
        self.assertIn("--draft=false", workflow["jobs"]["publish"]["steps"][0]["run"])


if __name__ == "__main__":
    unittest.main()
