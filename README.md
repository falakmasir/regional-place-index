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
   workflow**. Select **seattle**, leave release publication **off**, and run.
4. Review the per-stage job summary and download `measurements-washington-seattle`.
   A successful small build also produces a one-day `dataset-washington-seattle`
   review artifact. The `.tar.gz.json` file reports feature/object counts, stored
   bytes, archive bytes and SHA-256. Record cold/warm runner conditions accurately:
   this version does not cache raw PBFs.
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
No raw PBF or local SQLite scratch database is published.

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
manual catch-up. There is no claim of a daily national SLA.

## Why these stages differ from a restaurant scraper

1. Stream one Geofabrik extract, verify its published MD5 checksum, record SHA-256
   and PBF replication/header information. Two download attempts share one total
   deadline. A changed/latest checksum mismatch is a failure/retry, not accepted data.
2. Use native **`osmium tags-filter` with referenced objects retained** before Python
   resolves geometry. This avoids constructing Python objects/areas for the whole
   unfiltered state. Check node/way/relation references before deleting the raw file.
3. Resolve filtered nodes, ways and assembled polygon relations. Representative
   points are on/in source geometry, including polygons with holes—not a bounding-box
   center or a surveyed entrance. Repair invalid geometry explicitly and count it.
4. Apply the benchmark rectangle to the final representative point. Nodes, ways
   and relations keep distinct IDs. Missing selected polygon assemblies fail the
   build; unsupported non-area relations are counted, not invented as point venues.
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
street or national basemap**. The prototype uses fresh regional snapshots, not
an incomplete POI-only diff implementation. Reproducible indexes do not imply all
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

Install `osmium-tool` to include the native filtering test; otherwise that one test
is explicitly skipped. Geometry tests still use real libosmium/Shapely, including
concave polygons, holes, typed-ID collisions, closure, chains, scope, corruption,
page bounds, deterministic bundles, downloads and timeouts. Synthetic fixtures
are only for tests; do not run them as published regional builds.

Geofabrik: https://download.geofabrik.de/north-america/us.html
Osmium: https://osmcode.org/osmium-tool/manual.html
GitHub limits: https://docs.github.com/en/actions/reference/limits
See `DATA_LICENSE.md` for OSM/ODbL obligations and `LICENSE` for the original code.
