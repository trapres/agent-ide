"""Explicitly configured, pinned gitdiffviz adapter for one captured file pair."""
import hashlib
import json
import math
import os
from pathlib import Path
import re
import select
import signal
import stat
import subprocess
import tempfile
import time

from .exports import ExportError, evidence_export, digest

SOURCE_REVISION = '1c5639469fdbadefca5b7dc4a93f260648d90ee1'
ADAPTER_VERSION = '1'
OUTPUT_LIMIT = 8 * 1024 * 1024
LOG_LIMIT = 1024 * 1024
TEMP_LIMIT = 64 * 1024 * 1024
BINARY_LIMIT = 32 * 1024 * 1024


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ExportError('gitdiffviz JSON contains duplicate keys')
            result[key] = value
        return result
    def invalid(value):
        raise ExportError('gitdiffviz JSON contains nonfinite numbers')
    def floating(value):
        parsed = float(value)
        if not math.isfinite(parsed):
            return invalid(value)
        return parsed
    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid, parse_float=floating)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ExportError('gitdiffviz JSON is invalid') from exc


def configuration(path, check=lambda: None):
    if path is None:
        raise ExportError('gitdiffviz is disabled; supply --gitdiffviz-config')
    fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ExportError('gitdiffviz configuration must be a regular file')
        raw = stream.read(4097)
    if len(raw) > 4096:
        raise ExportError('gitdiffviz configuration exceeds 4 KiB')
    config = strict_json(raw)
    if not isinstance(config, dict) or set(config) != {'schema_version', 'binary', 'sha256', 'source_revision'}:
        raise ExportError('invalid gitdiffviz configuration fields')
    if type(config['schema_version']) is not int or config['schema_version'] != 1 or config['source_revision'] != SOURCE_REVISION:
        raise ExportError('unsupported gitdiffviz configuration/source revision')
    if not isinstance(config['binary'], str) or not Path(config['binary']).is_absolute() or not isinstance(config['sha256'], str) or not re.fullmatch('[0-9a-f]{64}', config['sha256']):
        raise ExportError('gitdiffviz requires an absolute binary and SHA-256 pin')
    binary = Path(config['binary'])
    fd = os.open(str(binary), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > BINARY_LIMIT or not os.access(str(binary), os.X_OK):
            raise ExportError('gitdiffviz binary must be a bounded executable regular file')
        digest = hashlib.sha256()
        for chunk in iter(lambda: stream.read(65536), b''):
            check()
            digest.update(chunk)
    if digest.hexdigest() != config['sha256']:
        raise ExportError('gitdiffviz binary SHA-256 does not match configuration')
    return config


def snapshot_binary(config, destination, check):
    fd = os.open(config['binary'], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    digest, size = hashlib.sha256(), 0
    with os.fdopen(fd, 'rb') as source, destination.open('xb') as target:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise ExportError('gitdiffviz binary changed after validation')
        for chunk in iter(lambda: source.read(65536), b''):
            check()
            size += len(chunk)
            if size > BINARY_LIMIT:
                raise ExportError('gitdiffviz binary exceeds snapshot limit')
            digest.update(chunk)
            target.write(chunk)
    if digest.hexdigest() != config['sha256']:
        raise ExportError('gitdiffviz binary changed after validation')
    destination.chmod(0o500)


def temp_usage(root):
    total, count = 0, 0
    for base, directories, files in os.walk(str(root), followlinks=False):
        for name in directories + files:
            count += 1
            try:
                info = (Path(base) / name).lstat()
            except FileNotFoundError:
                # Atomic writers may rename a temporary file between walk/stat.
                continue
            if stat.S_ISLNK(info.st_mode):
                raise ExportError('gitdiffviz temporary output contains a symlink')
            if stat.S_ISREG(info.st_mode):
                total += info.st_size
            if count > 256 or total > TEMP_LIMIT:
                raise ExportError('gitdiffviz temporary output exceeds limits')


def run(command, cwd, env, check, root, data=b''):
    """Bound pipe memory, runtime and owned descendants; never expose raw logs."""
    with tempfile.TemporaryFile() as input_file:
        input_file.write(data)
        input_file.seek(0)
        process = subprocess.Popen(command, cwd=str(cwd), env=env, stdin=input_file,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        output, size = [], 0
        pipes = [process.stdout, process.stderr]
        deadline = time.monotonic() + 10
        try:
            while pipes:
                check()
                if time.monotonic() >= deadline:
                    raise ExportError('gitdiffviz subprocess deadline exceeded')
                temp_usage(root)
                ready, _, _ = select.select(pipes, [], [], .02)
                for pipe in ready:
                    chunk = os.read(pipe.fileno(), 65536)
                    if not chunk:
                        pipes.remove(pipe)
                    else:
                        size += len(chunk)
                        if size > LOG_LIMIT:
                            raise ExportError('gitdiffviz subprocess output exceeds limits')
                        if pipe is process.stdout:
                            output.append(chunk)
            while process.poll() is None:
                check()
                if time.monotonic() >= deadline:
                    raise ExportError('gitdiffviz subprocess deadline exceeded')
                temp_usage(root)
                time.sleep(.01)
            if process.returncode:
                raise ExportError('gitdiffviz subprocess failed (exit %d); terminal evidence remains available' % process.returncode)
            check()
            temp_usage(root)
            return b''.join(output)
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.stdout.close()
            process.stderr.close()
            process.wait(timeout=1)


def output_json(path):
    fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > OUTPUT_LIMIT:
            raise ExportError('gitdiffviz document exceeds output limit')
        raw = stream.read(OUTPUT_LIMIT + 1)
    if len(raw) > OUTPUT_LIMIT:
        raise ExportError('gitdiffviz document exceeds output limit')
    value = strict_json(raw)
    if not isinstance(value, dict) or type(value.get('version')) is not int or value['version'] != 1:
        raise ExportError('unsupported gitdiffviz output version')
    return value


def reuse(cache, request_key, check):
    if not cache.path.exists():
        return None
    with cache.locked():
        cache.clean()
        entries = cache.entries()
        while entries and (len(entries) > cache.count or sum(i.st_size for _, i in entries) > cache.budget):
            entries.pop(0)[0].unlink()
        for path, info in entries:
            check()
            if path.suffix != '.json':
                continue
            if info.st_size > cache.artifact_limit:
                continue
            try:
                document = strict_json(path.read_bytes())
                core = {k: document[k] for k in ('provenance', 'evidence')}
                if document.get('schema_version') != 1 or document['artifact'].get('media_type') != 'application/json' or document['artifact'].get('id') != path.stem:
                    continue
                if core['provenance'].get('options', {}).get('request_sha256') != request_key:
                    continue
                if digest(core, check) != path.stem:
                    continue
                provenance = dict(core['provenance'])
                provenance.pop('synthetic_comparison', None)
                provenance['options'] = dict(provenance['options'])
                provenance['options'].pop('request_sha256', None)
                evidence = dict(core['evidence'])
                evidence.pop('gitdiffviz', None)
                if digest({'provenance':provenance, 'evidence':evidence}, check) == request_key:
                    return cache.result(path, True, core['provenance'])
            except (KeyError, TypeError, AttributeError):
                continue
            except ExportError as exc:
                if str(exc) == 'gitdiffviz JSON is invalid' or str(exc) == 'export exceeds artifact byte limit':
                    continue
                raise
    return None


def gitdiffviz_export(directory, records, row, revision, cache, config_path, check=lambda: None):
    if row['kind'] != 'effect' or row['payload'].get('operation') not in ('file.create', 'file.modify', 'file.delete'):
        raise ExportError('gitdiffviz requires a selected captured file effect')
    config = configuration(config_path, check)
    # Capture exactly the existing granted evidence without publishing an interim artifact.
    class Capture:
        path = cache.path
        def publish(self, core, check):
            return core
    core = evidence_export(directory, records, row, revision, Capture(), check)
    import base64
    before, after = core['evidence']['before'], core['evidence']['after']
    for side in (before, after):
        if side['state'] not in ('available', 'absent') or side.get('stale'):
            raise ExportError('gitdiffviz requires complete non-stale captured before/after evidence')
        if side.get('mode') not in (None, '100644', '100755'):
            raise ExportError('gitdiffviz does not support captured symlink effects; use JSON evidence')
    path = row['payload']['path']
    if any(part.casefold() == '.git' for part in path.split('/')) or any(ord(c) < 32 or 0xd800 <= ord(c) <= 0xdfff for c in path):
        raise ExportError('gitdiffviz does not support this historical filename; use JSON evidence')
    core['provenance'].update(exporter='labradour-gitdiffviz', exporter_version=ADAPTER_VERSION,
        options={'source_revision':SOURCE_REVISION, 'binary_sha256':config['sha256'],
                 'scope':'selected-file-only', 'semantics':'structural-only'})
    core['provenance']['limitations'] += ['Synthetic selected-file commits are not original project/checkpoint commits.',
        'Scene is structural only; semantic extraction and graphical opening are not implemented.']
    request_key = digest(core, check)
    cached = reuse(cache, request_key, check)
    if cached:
        return cached
    core['provenance']['options']['request_sha256'] = request_key
    with tempfile.TemporaryDirectory(prefix='labradour-gitdiffviz-') as temporary:
        root = Path(temporary)
        binary = root / 'gitdiffviz'
        snapshot_binary(config, binary, check)
        repo = root / 'captured'
        repo.mkdir(mode=0o700)
        env = {'PATH': os.environ.get('PATH', os.defpath), 'HOME': str(root), 'TMPDIR': str(root),
               'PYTHONDONTWRITEBYTECODE': '1',
               'LC_ALL': 'C', 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull,
               'GIT_NO_REPLACE_OBJECTS': '1', 'GIT_TERMINAL_PROMPT': '0',
               'GIT_AUTHOR_NAME': 'Captured evidence', 'GIT_AUTHOR_EMAIL': 'captured@localhost',
               'GIT_COMMITTER_NAME': 'Captured evidence', 'GIT_COMMITTER_EMAIL': 'captured@localhost',
               'GIT_AUTHOR_DATE': '2000-01-01T00:00:00+0000', 'GIT_COMMITTER_DATE': '2000-01-01T00:00:00+0000'}
        def invoke(*args, data=b''):
            return run(list(args), repo, env, check, root, data)
        invoke('git', 'init', '--quiet', '--template=')
        invoke('git', 'config', 'core.quotePath', 'false')
        commits = []
        for side in (before, after):
            invoke('git', 'read-tree', '--empty')
            if side['state'] == 'available':
                data = base64.b64decode(side['content_base64'])
                oid = invoke('git', 'hash-object', '-w', '--stdin', data=data).decode().strip()
                invoke('git', 'update-index', '--add', '--cacheinfo', side['mode'] + ',' + oid + ',' + path)
            tree = invoke('git', 'write-tree').decode().strip()
            args = ['git', 'commit-tree', tree] + (['-p', commits[-1]] if commits else [])
            commits.append(invoke(*args, data=b'Captured file state\n').decode().strip())
        if after['state'] == 'available':
            destination = repo / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(base64.b64decode(after['content_base64']))
        diff_path, scene_path = root / 'diff.json', root / 'scene.json'
        invoke(str(binary), 'extract-diff', '--repo', str(repo), '--base', commits[0], '--target', commits[1], '--out', str(diff_path))
        diff = output_json(diff_path)
        if diff.get('comparison') != {'base': commits[0], 'target': commits[1]} or not isinstance(diff.get('files'), list) or len(diff['files']) > 1 or any(not isinstance(f, dict) or f.get('path') != path for f in diff['files']):
            raise ExportError('gitdiffviz diff does not match the granted comparison')
        invoke(str(binary), 'build-scene', '--diff', str(diff_path), '--out', str(scene_path))
        scene = output_json(scene_path)
        body = scene.get('scene', {})
        allowed = {path, ''} | {str(p) for p in Path(path).parents if str(p) != '.'}
        if scene.get('comparison') != diff['comparison'] or not isinstance(body, dict) or not isinstance(body.get('nodes'), list) or not isinstance(body.get('edges'), list) or any(not isinstance(n, dict) or n.get('path', '') not in allowed for n in body['nodes']):
            raise ExportError('gitdiffviz scene does not match the granted comparison')
        # Temporary paths are implementation details, not persistent scene locations.
        diff['repoRoot'] = scene['repoRoot'] = 'captured-effect'
        core['evidence']['gitdiffviz'] = {'diff': diff, 'scene': scene}
        core['provenance']['synthetic_comparison'] = {'base':commits[0], 'target':commits[1]}
        check()
        return cache.publish(core, check)
