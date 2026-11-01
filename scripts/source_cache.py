"""Cache-first regional checkpoints, with cold-download fallback and offline replay."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import time
from urllib.parse import urlsplit

MAX_BYTES = 3_000_000_000


class SourceUnavailable(ValueError):
    pass


def save(path, value):
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(value, sort_keys=True, separators=(',', ':')) + '\n')
    temp.replace(path)


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def read_header(path):
    import osmium
    from osmium.replication.utils import get_replication_header
    state = get_replication_header(str(path))
    with osmium.io.Reader(str(path), osmium.osm.NOTHING) as reader:
        history = reader.header().has_multiple_object_versions
    return {'url': state.url, 'sequence': state.sequence,
            'timestamp': state.timestamp.isoformat() if state.timestamp else None,
            'history': history}


def replication_url(header, source_url, require_timestamp=False):
    expected = 'https://download.geofabrik.de' + urlsplit(source_url).path.removesuffix('-latest.osm.pbf') + '-updates'
    url = header.get('url')
    parts = urlsplit(url or '')
    if header.get('history'):
        raise SourceUnavailable('History files are not supported as regional checkpoints')
    if not url and not require_timestamp:
        return None  # Verified snapshots without replication headers are reusable offline.
    # A legacy HTTP header is normalized to HTTPS before running the updater.
    if (parts.scheme not in ('http', 'https') or parts.hostname != 'download.geofabrik.de'
            or parts.username or parts.password or parts.port not in (None, 80, 443)
            or parts.query or parts.fragment
            or ('https://' + str(parts.hostname) + parts.path).rstrip('/') != expected):
        raise SourceUnavailable('Checkpoint replication URL does not match this regional source')
    if require_timestamp and not header.get('timestamp'):
        raise SourceUnavailable('Checkpoint has no replication timestamp; use an explicit verified seed')
    return expected


def validate_checkpoint(region, url, work):
    path, info = work / 'source.osm.pbf', work / 'source.json'
    if not path.is_file() or not info.is_file():
        raise SourceUnavailable('No cached regional checkpoint was restored')
    meta = json.loads(info.read_text())
    if (meta.get('checkpoint_schema') != 1 or meta.get('region') != region or meta.get('url') != url
            or meta.get('verification') not in ('snapshot_md5', 'operator_seed_sha256', 'replication_from_verified_checkpoint')
            or not meta.get('way_nodes_verified')
            or not re.fullmatch(r'[0-9a-f]{64}', meta.get('sha256', ''))
            or not 0 < path.stat().st_size <= MAX_BYTES
            or path.stat().st_size != meta.get('bytes') or sha256(path) != meta['sha256']):
        raise SourceUnavailable('Cached checkpoint metadata/hash/region is invalid')
    header = read_header(path)
    replication_url(header, url)
    if meta.get('replication') != header:
        raise SourceUnavailable('Cached header and metadata disagree')
    return meta, header


def check_way_nodes(path):
    subprocess.run(['osmium', 'check-refs', str(path)], check=True, timeout=600)


def update_checkpoint(path, header, url, work):
    expected = replication_url(header, url, require_timestamp=True)
    before_time = dt.datetime.fromisoformat(header['timestamp'])
    if before_time.tzinfo is None:
        raise SourceUnavailable('Replication timestamp has no timezone')
    age = dt.datetime.now(dt.timezone.utc) - before_time
    if age > dt.timedelta(days=90) or age < -dt.timedelta(days=1):
        raise SourceUnavailable('Checkpoint is outside the safe incremental-update window')
    candidate = work / 'updated.osm.pbf'
    normalized = work / 'normalized.osm.pbf'
    candidate.unlink(missing_ok=True)
    normalized.unlink(missing_ok=True)
    extra_copies = 2 if header['url'] != expected else 1
    if shutil.disk_usage(work).free < path.stat().st_size * extra_copies + 1024**3:
        raise SourceUnavailable('Insufficient disk reserve for an atomic PBF update')
    try:
        source = path
        if header['url'] != expected:
            # pyosmium rejects a --server differing from the input header. Use
            # native copying, not a Python scan, to upgrade HTTP/trailing-slash
            # headers while preserving all objects and replication state.
            subprocess.run(['osmium', 'cat', str(path),
                            '--output-header=osmosis_replication_base_url=' + expected,
                            '--output-header=osmosis_replication_timestamp!',
                            '--output-header=osmosis_replication_sequence_number!',
                            '-o', str(normalized)], check=True, timeout=600)
            source = normalized
        result = subprocess.run(
            ['pyosmium-up-to-date', str(source), '--server', expected,
             '--size', '256', '--socket-timeout', '30', '--tmpdir', str(work),
             '--outfile', str(candidate), '-v'], timeout=1200)
        # Exit 1 means only PART of the changes may have been applied. Never
        # advance the saved checkpoint or silently call that current.
        if result.returncode != 0:
            raise SourceUnavailable(f'Replication update incomplete/failed (exit {result.returncode}); cached base preserved')
        target = candidate if candidate.exists() else source
        after = read_header(target)
        replication_url(after, url, require_timestamp=True)
        after_time = dt.datetime.fromisoformat(after['timestamp'])
        if (after_time < before_time or (header.get('sequence') is not None
                and (after.get('sequence') is None or after['sequence'] < header['sequence']))):
            raise SourceUnavailable('Replication update would move backwards')
        if not 0 < target.stat().st_size <= MAX_BYTES:
            raise SourceUnavailable('Updated checkpoint exceeds its size limit')
        if target != path:
            check_way_nodes(target)
        # Do not replace the old checkpoint until every update check succeeds.
        return target, after
    except BaseException:
        candidate.unlink(missing_ok=True)
        normalized.unlink(missing_ok=True)
        raise


def prepare(region, url, work, mode='auto', allow_full_download=True,
            full_download=None, seed_file=None, seed_sha256=None):
    if mode not in ('auto', 'reuse', 'update', 'fresh', 'seed'):
        raise ValueError('Unknown source mode')
    parts = urlsplit(url)
    if (parts.scheme != 'https' or parts.hostname != 'download.geofabrik.de'
            or parts.username or parts.password or parts.port not in (None, 443)
            or not parts.path.endswith('-latest.osm.pbf') or parts.query or parts.fragment):
        raise ValueError('Invalid Geofabrik source URL')
    work.mkdir(parents=True, exist_ok=True)
    state_path = work / 'cache-state.json'
    ready = work / 'cache-ready.json'
    ready.unlink(missing_ok=True)
    state = {'schema': 1, 'mode': mode, 'region': region, 'status': 'failed',
             'cache_hit': False, 'cache_changed': False, 'full_download_performed': False}
    path = work / 'source.osm.pbf'
    info = work / 'source.json'
    candidate = normalized = None
    try:
        cached, header, cache_error = None, None, None
        if mode in ('auto', 'reuse', 'update'):
            try:
                cached, header = validate_checkpoint(region, url, work)
                state['cache_hit'] = True
            except (SourceUnavailable, OSError, ValueError, RuntimeError) as exc:
                cache_error = str(exc)
        if mode == 'reuse' or (mode == 'auto' and cached is not None):
            if cached is None:
                raise SourceUnavailable((cache_error or 'No cache') +
                    '. Reuse mode never downloads. Seed an existing PBF, or explicitly allow one fresh download.')
            print('CACHE REUSE: validated regional base; no Geofabrik requests.', flush=True)
            state['result'] = 'cached_snapshot_reused'
            meta = cached
        elif mode == 'seed':
            if not seed_file or not seed_sha256 or not re.fullmatch(r'[0-9a-f]{64}', seed_sha256):
                raise SourceUnavailable('Seed mode requires an existing PBF and its explicit SHA-256')
            seed_file = Path(seed_file)
            if seed_file.resolve() == path.resolve():
                raise SourceUnavailable('Seed must be separate from the active checkpoint')
            if not 0 < seed_file.stat().st_size <= MAX_BYTES or sha256(seed_file) != seed_sha256:
                raise SourceUnavailable('Seed file size/SHA-256 does not match')
            header = read_header(seed_file)
            replication_url(header, url)
            check_way_nodes(seed_file)
            if shutil.disk_usage(work).free < seed_file.stat().st_size + 1024**3:
                raise SourceUnavailable('Insufficient reserve to import seed')
            candidate = work / 'seeded.osm.pbf'
            shutil.copyfile(seed_file, candidate)
            if sha256(candidate) != seed_sha256:
                raise SourceUnavailable('Seed changed during import')
            candidate.replace(path)
            meta = {'region': region, 'url': url, 'bytes': path.stat().st_size,
                    'sha256': seed_sha256, 'checksum_verified': False,
                    'verification': 'operator_seed_sha256', 'verified_seed_sha256': seed_sha256}
            state.update(result='existing_seed_imported', cache_changed=True)
            print('CACHE SEED: imported operator-supplied PBF; no Geofabrik download.', flush=True)
        else:
            refresh_reason = cache_error
            if mode == 'update' and cached is not None:
                try:
                    target, after = update_checkpoint(path, header, url, work)
                    candidate = target if target != path else None
                    normalized = work / 'normalized.osm.pbf'
                    new_hash = sha256(target)
                    changed = new_hash != cached['sha256']
                    meta = dict(cached)
                    if changed:
                        meta.update(bytes=target.stat().st_size, sha256=new_hash,
                                    checksum_verified=False, verification='replication_from_verified_checkpoint',
                                    previous_checkpoint_sha256=cached['sha256'])
                        meta.setdefault('verified_seed_sha256', cached.get('verified_snapshot_sha256', cached['sha256']))
                        # Re-encoded updated bytes cannot match the upstream
                        # snapshot MD5. Never keep an old MD5 as a new check.
                        meta.pop('md5', None)
                        if candidate:
                            candidate.replace(path)
                    header = after
                    state.update(result='replication_updated' if changed else 'no_new_diffs', cache_changed=changed)
                    print('CACHE UPDATE: ' + state['result'] + '; no full snapshot download.', flush=True)
                except (SourceUnavailable, subprocess.SubprocessError) as exc:
                    refresh_reason = str(exc)
                    if not allow_full_download:
                        raise SourceUnavailable(refresh_reason + '. Full downloads are disabled; retry reuse mode or explicitly seed/refresh.') from exc
                    cached = None
            if mode == 'fresh' or cached is None:
                if not allow_full_download:
                    raise SourceUnavailable((refresh_reason or 'Fresh snapshot requested') +
                        '. Full downloads are disabled. Supply an existing seed or explicitly enable one download.')
                if full_download is None:
                    raise ValueError('No full-download implementation supplied')
                print('FULL DOWNLOAD explicitly authorized: ' + (refresh_reason or mode), flush=True)
                state['full_download_performed'] = True
                full_download(region, work)
                meta = json.loads(info.read_text())
                if (meta.get('region') != region or meta.get('url') != url or not meta.get('checksum_verified')
                        or not re.fullmatch(r'[a-f0-9]{32}', meta.get('md5', ''))
                        or not 0 < path.stat().st_size <= MAX_BYTES
                        or meta.get('sha256') != sha256(path) or meta.get('bytes') != path.stat().st_size):
                    raise SourceUnavailable('Fresh download did not produce verified source metadata')
                header = read_header(path)
                replication_url(header, url)
                check_way_nodes(path)
                meta.update(verification='snapshot_md5', verified_snapshot_sha256=meta['sha256'],
                            verified_snapshot_md5=meta['md5'])
                state.update(result='fresh_snapshot_downloaded', cache_changed=True)
        if state['cache_changed']:
            meta.update(checkpoint_schema=1, replication=header, way_nodes_verified=True)
            save(info, meta)
        state.update(status='ready', checkpoint_sha256=meta['sha256'], replication=header)
        save(state_path, state)
        # Only these two files are cached; reports and filtered/index data are not.
        save(ready, {'sha256': meta['sha256']})
        return state
    except BaseException as exc:
        state['error'] = str(exc)
        save(state_path, state)
        raise
    finally:
        for temp in (candidate, normalized, work / 'updated.osm.pbf', work / 'normalized.osm.pbf', work / 'seeded.osm.pbf'):
            if temp and temp != path:
                temp.unlink(missing_ok=True)
