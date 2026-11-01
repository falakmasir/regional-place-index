# Regional Place Index

A standalone, public, offline OpenStreetMap POI-index builder and benchmark.
**This is a testable exporter, not a deployed search service or a proven national
performance result.** No application integrations, cloud credentials, website
crawler, chain blacklist, live Overpass queries, or database ingestion hooks.

## First GitHub test

1. Put **this directory's contents** at the root of a new public repository. Do
   not copy a parent repository, its history, configuration or other directories.
   Include the hidden `.github` directory. Enable Actions if prompted.
2. The **Exporter tests** workflow runs on push. It exercises real libosmium and
   Shapely geometry; hosted CI also checks native reference-preserving filtering.
3. On the default branch, open **Actions → Regional place-index benchmark → Run
   workflow**. Select **seattle**, leave release publication **off**. The default
   `source_mode=reuse` uses a previously saved Washington base without contacting
   Geofabrik. With no cache, it stops rather than downloading. For a first seed,
   use an existing PBF as described below; only if none exists, explicitly select
   `fresh` and check `allow_full_download` for one initial download.
4. Review the per-stage job summary and download `measurements-washington-seattle`.
   A successful small build also produces a one-day `dataset-washington-seattle`
   review artifact. The `.tar.gz.json` file reports feature/object counts, stored
   bytes, archive bytes and SHA-256. Record cold/warm runner conditions accurately:
   `cache-state.json` records reuse/update/full-download and persistence results.
5. Test **los-angeles**, **new-york** and **miami**, then **all-benchmarks**. New York
   runs both NY and NJ sources; overlapping OSM IDs are stable and must be deduped
   when joining regional outputs. These are explicit stress-test rectangles, not
   authoritative municipal/metro boundaries or application area selections.
6. For a whole-state test choose **full-regions** and enter `washington,california`.
   `all` selects the 50 states plus DC, **not US territories**. Do this only after
   examining smaller results: the national raw input is intentionally never placed
   on one runner. `socal` and `norcal` are optional smaller California sources.

No custom secrets are needed. Public standard hosted-runner compute is free, but
jobs have six-hour platform limits, finite disk/RAM, concurrency limits, artifact
storage allowances and no completion SLA. This workflow only permits public-repo
benchmark runs. The internal job ceiling is five hours, stage budgets are explicit,
and matrix concurrency is two. A longer timeout does not fix an inefficient stage.

### Publication and daily operation are opt-in

To retain a larger dataset, rerun on the reviewed default branch with
`publish_release=true`. Only the repository's own temporary GitHub token is used.
The workflow creates a **draft** release, uploads verified derived bundles, checks
that every planned asset exists, and publishes only after **all jobs succeed**.
A failed run leaves an unpublished draft; inspect/delete abandoned drafts manually.
No raw PBF or local SQLite scratch database is published by the dataset workflow.
Release uploads are blocked if benchmark geometry quarantine excluded any features; a partial review
bundle is not silently promoted as a complete catalog.

Review bundles are limited to 20 MB each and 200 MB aggregate per run and retained
for one day. Oversized bundles are not uploaded as Actions artifacts: rerun with
Releases for those datasets. Measurements are small and retained for seven days.
Repeated runs still consume artifact storage; review account usage and remove
unneeded artifacts. Releases are developer dataset distribution, not a map CDN.
Keep an intentional release retention policy rather than accumulating unlimited
snapshots; do not delete the last known-good dataset during a failed build.

Schedules do **nothing** until repository variable `ENABLE_DAILY_BUILDS=true`.
Set `DAILY_BENCHMARK` (default `seattle`), `DAILY_REGIONS` (used only by
`full-regions`, default `washington`) and optionally `PUBLISH_RELEASES=true`.
For daily national preparation, use `DAILY_BENCHMARK=full-regions` and
`DAILY_REGIONS=all` only after benchmarks pass. Schedules are best effort and can
be disabled after 60 days of repository inactivity; monitor freshness and use
manual catch-up. Schedules use incremental `update` mode; full-download fallback
is **off** unless `ALLOW_FULL_DOWNLOADS=true` is explicitly set. A missing, stale
or unusable checkpoint otherwise fails closed. There is no daily national SLA.

## Source checkpoints: retry without downloading Washington again

| Mode | Behavior | Full Geofabrik download |
| --- | --- | --- |
| `reuse` (manual default) | Validate and replay the saved snapshot offline | Never, even if the permission checkbox is checked |
| `update` | Restore the full regional base and apply regional replication diffs | Only with explicit `allow_full_download=true` on a miss/update failure |
| `fresh` | Replace the base with a checksum-verified snapshot using bounded curl | Requires explicit `allow_full_download=true` |
| `seed` | Import an existing PBF release asset with an operator-provided SHA-256 | Never |

The cache contains **only** `work/source.osm.pbf` and `work/source.json`. A new,
validated checkpoint is saved and its cache entry confirmed **before** native
filtering/geometry, so a later geometry failure does not discard the source.
A reuse/no-diff run does not upload another identical checkpoint. Restore uses a
region/OS/architecture prefix: a partial-key hit is valid and is not mistaken for
`cache-hit=false` meaning “no files.” Hash, size, schema, region, URL, header and
way-node verification provenance are checked before reuse. Nothing reads a raw
source out of a derived dataset bundle.

Updates use `pyosmium-up-to-date` on the **complete regional source**, not a
POI-only subset. HTTPS regional replication URLs are pinned to the configured
Geofabrik region; there is no planet-diff fallback. Legacy HTTP headers are
normalized while preserving timestamp/sequence. The original base survives failed,
partial, backwards, timed-out or reference-invalid updates. The updater has a
256 MB diff budget, 20-minute timeout and 90-day age guard; no unbounded catch-up.
A headerless verified base can be replayed offline but not incrementally updated.
Updated bytes get their own SHA-256 and replication provenance, never an inherited
snapshot MD5 or a false claim that transformed bytes match the original download.

### Import a base you already have (no new Geofabrik transfer)

If you retained the scraper's **full Washington regional base**, use it—not a
Seattle filtered extract. In the public data repository, upload that existing
file as `washington.osm.pbf` to a temporary release (the normal GitHub release
asset size limit applies). For example, using your own existing local file:

```sh
shasum -a 256 /path/to/washington.osm.pbf
gh release create source-seed /path/to/washington.osm.pbf --title 'Temporary source seed' --notes 'Existing OSM regional base; OpenStreetMap contributors, ODbL. See DATA_LICENSE.md.'
```

Run **seattle**, `source_mode=seed`, `seed_release_tag=source-seed`, and paste the
trusted lowercase SHA-256 into `seed_sha256`. Leave `allow_full_download` and
publication **off**. Only that named asset in the same repository is fetched.
The seed is verified/imported and cached before geometry work. Subsequent retries
use **reuse**, not seed/fresh. The importer rejects a conflicting regional header,
history files, wrong hashes and missing way-node dependencies. No asset is uploaded
or deleted automatically; review/remove your temporary release intentionally.
Operator-seed SHA-256 is recorded separately from upstream snapshot verification.

Actions caches are **not archival storage**: eviction, quota and branch scoping
apply. Retry on the same/default branch; a cache on an unrelated branch may not
be visible. Cache misses never authorize a full download by themselves. The
combined raw US sources exceed the standard 10 GB cache budget; this is not a
claim that an entire national cache will fit. Keep a trusted seed if you need
recovery without another provider transfer. Saving/confirming a cache cannot
prevent later service eviction or runner loss before the save completes.

When updating the public repository, upload **all revised source files including
`scripts/source_cache.py` and the hidden workflow**, then start a **new run on
that updated commit**. “Re-run jobs” on an old revision still executes the old,
uncached downloader. Previously discarded PBFs cannot be reconstructed from the
small measurement artifacts.

## Why these stages differ from a restaurant scraper

1. Restore and validate a complete cached regional PBF; reuse it offline, or update
   it using regional diffs. Only explicitly authorized full downloads use curl,
   verify published MD5 and record SHA-256/header information. Two attempts share
   one deadline. A changed/latest checksum mismatch is never accepted.
2. Use native **`osmium tags-filter` with referenced objects retained** before Python
   resolves geometry. This avoids constructing Python objects/areas for the whole
   unfiltered state. Missing nodes used by ways remain a hard failure. A separate
   relation audit records missing members, their relation IDs/types and whether
   they affect an indexed polygon. Regional extracts can omit relation members;
   those gaps do not automatically invalidate unrelated node/way POIs.
3. Resolve filtered nodes, ways and assembled polygon relations. Representative
   points are on/in source geometry, including polygons with holes—not a bounding-box
   center or a surveyed entrance. Repair invalid geometry explicitly and count it.
   Incomplete supported polygon relations still stop the build, even if an area
   assembler could return only their available rings. Nested relation gaps also
   propagate to affected parents; do not silently publish a partial polygon.
4. Apply the benchmark rectangle to the final representative point. Nodes, ways
   and relations keep distinct IDs. Missing reference members of supported polygons
   remain fatal. Unusable areas with complete references can be quarantined by the
   benchmark, with explicit quality metadata; unsupported non-area relations are
   counted, not invented as point venues.
5. Spool records locally into SQLite, then build spatial pages and sorted
   per-partition name/token-prefix postings. This SQLite file is disposable local
   working state, never a cloud database or published import.
6. Hash/verify every page and cross-reference, then publish a licensed, checksummed
   bundle. No independent network request per business. Chain branches remain
   separate. Explicit source closure/lifecycle/end-date tags are excluded; no claim
   that HTTP success or OSM presence proves a place is open or has a current offer.

End-date filtering records its UTC `closure_as_of` date in the output statistics;
use the export command's `--as-of YYYY-MM-DD` when reproducing a past build.

The supported category allowlist is in `scripts/pipeline.py`; it includes named
food venues, shops and selected navigation POIs, **not every OSM object, address,
street or national basemap**. Source replication updates the complete regional
base, then the derived indexes are rebuilt—not patched with incomplete POI-only diffs. Reproducible indexes do not imply all
underlying OSM businesses have accurate/current data.

This follows BBBike's published preparation/index-serving pattern. References:
[offline conversion and concurrency](https://github.com/severinmenard/bbbike-world/blob/9410d13eed262972909cf92d34a4b7cfa1132d1f/Makefile.osm),
[offline name aliases](https://github.com/severinmenard/bbbike-world/blob/9410d13eed262972909cf92d34a4b7cfa1132d1f/bin/opensearch-suggestion),
[local address lookup](https://github.com/eserte/bbbike/blob/1941b9fe7b5619bf12f08d501f7c2981e42cdd11/miscsrc/GeocoderAddr.pm).
These are inspected historical source revisions, not proof of the service's
current deployment or that its complete global production build uses free Actions.
No BBBike source code was copied into this exporter.

## Output format (prototype schema 1)

- `manifest.json`: exact source URL/checksums, header, scope, counts, exporter
  revision, attribution and references to directory pages. Maximum 512 KiB.
- `objects/<sha256>.json.gz`: deterministic gzip (mtime zero), SHA-256 over the
  compressed bytes, at most **64 KiB decoded** per page.
- Spatial records: zoom-12 cell, typed ID, source object version, name, point,
  retained tags, geometry semantics, `verification=source_tags_only`.
- Lexical postings: zoom-8 partition, sorted `[normalizedTerm, id, lon, lat,
  displayName, spatialPageHash]`. Unicode normalization is NFKD + casefold + removal
  of combining marks + whitespace collapse. Clients must use identical semantics;
  web/native query libraries are not delivered by this benchmark project.
- Directory pages: zoom-6 grouping, paged lists of cells and their page descriptors.
  Name descriptors include first/last terms. Resolve geographic partitions before
  lexical ranges/result caps; read all intersecting directory pages and paginate
  dense ranges instead of treating a partial result as empty.
- Up to 16 aliases per record; preserve the primary name before optional aliases.
  Aliases include bounded suffix-token variants, not every prefix string.

A consumer must validate manifest/page hashes, schema, size limits, record locators,
source trust and archive paths before use. Cross-region deduplication/promotion,
last-good per-region composition, serving routes and client query UI are **not**
implemented here. Immutable page hashes enable measuring changed-object churn;
this benchmark does not upload pages into any application storage.

## Measurements and failure behavior

Each stage records wall time, exit status, sampled process-tree RSS and filesystem
used/free high-water observations. Sampling is every 250 ms: it can miss short
peaks and filesystem use includes OS/tools, not just scratch data. Reported
measurements are not Cloudflare CPU measurements or billed private runner minutes.
Full downloads print native curl transfer progress, and measured stages print a resource heartbeat about every 30 seconds so a long stage is not
silent. Timing out is distinct from a data-integrity failure.

The measurement artifact includes `cache-state.json`, `relation-audit.json` and
`geometry-report.json`, even when geometry
export fails later. Missing relation members are diagnostic unless they affect a
supported polygon candidate. The audit is bound to the filtered file's SHA-256
and closure date so it cannot be accidentally reused for a different input.

The monitor stops a stage if sampled RSS exceeds 12 GiB or free disk drops below
1 GiB. Downloads require 2 GB free reserve and have a 3 GB input cap.

Failed stages do not publish a complete release. Compact measurements are attempted
with `always()` even after failure; runner loss/hard cancellation can still prevent
uploads. Exporter/tool versions, logs and counts make regressions inspectable.

Compare at least a week of outputs before promising daily national operation:
source/download load, worst stage, aggregate runner-minutes, peak memory/disk,
filtered feature counts, bytes, page/object count and changed-object churn.

## Local tests (Python 3.11, Linux build tools)

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m unittest discover -s tests -v
```

Install `osmium-tool` to include native filtering/header-normalization checks;
otherwise those native checks are explicitly skipped. Geometry tests still use real libosmium/Shapely, including
concave polygons, holes, typed-ID collisions, closure, chains, scope, corruption,
page bounds, deterministic bundles, downloads and timeouts. Synthetic fixtures
are only for tests; do not run them as published regional builds.

Geofabrik: https://download.geofabrik.de/north-america/us.html
Osmium: https://osmcode.org/osmium-tool/manual.html
GitHub limits: https://docs.github.com/en/actions/reference/limits
See `DATA_LICENSE.md` for OSM/ODbL obligations and `LICENSE` for the original code.


## Invalid geometry: strict exports versus benchmark quarantine

libosmium may raise `RuntimeError: invalid area` **before** it produces WKB; Shapely
cannot repair a geometry that does not exist. The exporter catches that specific
conversion failure and records the typed source ID, area ID, name and error.
Unexpected runtime errors are not treated as harmless data defects.

Direct `export` commands are strict by default. The benchmark workflow explicitly
uses `--quarantine-invalid-geometry`, so valid records can be measured while bad
areas are excluded. The default maximum is 100 rejected features; exceeding it
fails the build rather than allowing unlimited data loss. Missing way nodes and
missing source members of supported polygons remain fatal in both modes.

- Once geometry parsing starts, `geometry-report.json` is saved on success or
  failure and included with metrics, bound to the filtered-input hash/date.
- Quarantined IDs are removed from both records and name postings, including any
  earlier closed-way fallback. No bbox-center or fabricated point is substituted.
- Valid Shapely repairs are marked; a collapsed polygon is not accepted as a line
  or point merely because `make_valid` returned something nonempty.
- `manifest.json` includes `coverage.geometry_complete` and an exclusion count.
  This describes geometry processing, not a guarantee of exhaustive real-world
  place coverage. Rejected areas may lie outside the requested benchmark bbox;
  without usable geometry their relationship to that bbox can be unknown.
- Review bundles include the report; verification cross-checks its counts and
  makes sure rejected IDs are absent. Public dataset Release uploads are blocked
  whenever exclusions exist. Inspect/resolve them before production promotion.

A passed benchmark with quarantine means “valid subset processed and measured,”
not “every requested place was successfully indexed.”
