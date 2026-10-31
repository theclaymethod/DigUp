#!/usr/bin/env python3
"""File-authored design collections on DigUp's native indexing and search engine."""
import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit

REPO = Path(__file__).resolve().parents[2]
MEDIA = set('png jpg jpeg webp gif heic heif avif tiff tif bmp svg pdf txt text md markdown rtf doc docx odt html htm srt vtt mp4 mov m4v webm mkv avi mp3 wav m4a aac ogg flac opus'.split())
IMAGES = set('png jpg jpeg webp gif heic heif avif tiff tif bmp svg'.split())
CODE = set('py swift ts tsx js jsx mjs cjs mts c h cpp cc hpp m mm rs go java kt dart vue svelte css scss sass glsl wgsl metal hlsl sh bash sql ipynb'.split())
SKIP = set('node_modules Pods .git .venv venv build dist .expo .cache __pycache__ DerivedData vendor Vendor third_party target coverage'.split())
SECRET = re.compile(r'^(?:\.|secret|credential|id_rsa|id_ed25519)|\.(?:pem|key|p12|pfx|crt|cer|keystore)$', re.I)


def read(path, default=None):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default


def atomic(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def save(path, value):
    atomic(path, (json.dumps(value, ensure_ascii=False, sort_keys=True) + '\n').encode())


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def stamp(path):
    value = path.stat()
    return [value.st_size, value.st_mtime_ns, value.st_ino, value.st_dev, value.st_ctime_ns]


@contextlib.contextmanager
def interruptible():
    def stop(signum, frame):
        raise KeyboardInterrupt()
    previous = signal.signal(signal.SIGTERM, stop) if threading.current_thread() is threading.main_thread() else None
    try:
        yield
    finally:
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)


def safe_file(folder, relative):
    rel = Path(relative)
    if rel.is_absolute() or '..' in rel.parts or not rel.parts:
        raise ValueError('File escapes its entry')
    path = folder
    for part in rel.parts:
        path /= part
        if path.is_symlink():
            raise ValueError('Symlink sources are not indexed')
    if not path.resolve().is_relative_to(folder.resolve()):
        raise ValueError('File escapes its entry')
    return path


def catalog(root):
    result = []
    for folder in sorted((root / 'entries').iterdir()):
        if not folder.is_dir() or folder.is_symlink():
            continue
        if not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', folder.name):
            raise ValueError('Invalid entry ID')
        manifest = safe_file(folder, 'entry.json')
        entry = read(manifest)
        if not isinstance(entry, dict) or not isinstance(entry.get('title'), str):
            raise ValueError('Invalid entry manifest: ' + folder.name)
        state = entry.get('libraryState')
        if state is not None and (not isinstance(state, dict) or state.get('status') != 'active'):
            continue
        if entry.get('tier', 'private') not in {'private', 'gallery', 'portfolio'}:
            raise ValueError('Invalid visibility: ' + folder.name)
        result.append({**entry, 'id': folder.name, '_manifest': digest(manifest)})
    return result


def sources(folder):
    for current, dirs, files in os.walk(folder, followlinks=False):
        here = Path(current)
        dirs[:] = sorted(d for d in dirs if d not in SKIP and not SECRET.search(d) and not (here / d).is_symlink())
        for name in sorted(files):
            if SECRET.search(name) or name.endswith(('.min.js', '.min.css')):
                continue
            path = here / name
            if path.is_file() and not path.is_symlink() and path.suffix.lower().lstrip('.') in MEDIA | CODE:
                yield path.relative_to(folder).as_posix()


def metadata(root, entry, public=False):
    fields = [entry['title'], entry.get('summary', ''),
              'Topics: ' + ', '.join(entry.get('tags', [])),
              'Platforms: ' + ', '.join(entry.get('platforms', []))]
    if not public:
        fields += ['Collection: ' + entry.get('collection', ''), 'Kind: ' + entry.get('kind', '')]
    for source in entry.get('sources', []):
        if public and (source.get('role') == 'capture' or urlsplit(source.get('url', '')).scheme not in {'http', 'https'}):
            continue
        fields.append(' '.join(str(source.get(key, '')) for key in ('title', 'author', 'url', 'license')))
    if not public or entry.get('publish', {}).get('notes'):
        notes = safe_file(root / 'entries' / entry['id'], 'notes.md')
        if notes.exists():
            fields.append(notes.read_text())
    return '\n\n'.join(fields).encode()


class Collection:
    def __init__(self, root, index=None, binary=None):
        self.root = Path(root).expanduser().resolve()
        if not (self.root / 'entries').is_dir():
            raise ValueError('Choose a canonical design directory containing entries/')
        self.index = Path(index).expanduser().resolve() if index else self.root / '.generated/digup'
        self.index.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.index.chmod(0o700)
        self.binary = Path(binary or os.environ.get('DESIGN_DIGUP_BINARY', REPO / '.build/release/digup')).expanduser().resolve()
        code = self.index / 'corpus/code'
        code.mkdir(parents=True, exist_ok=True, mode=0o700)
        # An isolated snapshot must not inherit its canonical repository's .generated ignore rule.
        if not (code / '.git').is_dir():
            subprocess.run(['git', 'init', '--quiet', str(code)], check=True, stdout=subprocess.DEVNULL)
        self.manifest = self.index / 'collection.json'
        self.indexed = self.index / 'indexed.json'
        self.child = None
        self.lines = queue.Queue()
        self.serial = 0
        self.lock = threading.RLock()
        self.generation = None
        state = read(self.manifest, {})
        if state and state.get('root') != str(self.root):
            raise ValueError('This derived index belongs to another collection')

    @contextlib.contextmanager
    def writer(self):
        with (self.index / 'collection.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError('Another design index writer is running') from None
            yield

    def prepare(self):
        previous = read(self.manifest, {})
        known = previous.get('files', {})
        if previous.get('version') == 1 and not self.indexed.exists():
            self.save_indexed({path: {**info, 'legacy': True} for path, info in known.items()
                               if info['lane'] != 'public'})
        by_source = {self.logical(info): (path, info) for path, info in known.items()}
        records = catalog(self.root)
        files = {}
        changed = 0

        def put(entry, rel, public=False, payload=None):
            nonlocal changed
            folder = self.root / 'entries' / entry['id']
            source = safe_file(folder, rel) if payload is None else None
            lane = 'public' if public else 'code' if source and source.suffix.lower().lstrip('.') in CODE else 'media'
            name = Path(rel)
            if source and source.suffix.lower().lstrip('.') in IMAGES:
                name = name.with_name('Screenshot-' + name.name)
            info = {'entry': entry['id'], 'source': rel if source else 'entry.json',
                    'lane': lane, 'metadata': payload is not None, 'manifest': entry['_manifest']}
            old_path, old = by_source.get(self.logical(info), (None, {}))
            before = stamp(source) if source else None
            # A full source stamp is a fast reuse guard, never evidence that native rows advanced.
            reuse = source and old.get('stamp') == before and old_path and Path(old_path).is_file()
            sha = old['sha'] if reuse else digest(source) if source else hashlib.sha256(payload).hexdigest()
            target = self.index / 'corpus' / lane / entry['id'] / ('metadata' if payload is not None else 'files') / sha / name
            self.snapshot_path(str(target), {**info, 'sha': sha})
            if target.exists():
                if digest(target) != sha:
                    raise ValueError('Corrupt content-addressed snapshot; rebuild this derived index')
            elif source:
                fd, temp = tempfile.mkstemp(dir=self.index)
                try:
                    os.close(fd)
                    shutil.copy2(source, temp)
                    if digest(Path(temp)) != sha or safe_file(folder, rel) != source or stamp(source) != before:
                        raise ValueError('Source changed during capture; rerun prepare')
                    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    os.chmod(temp, 0o600)
                    os.replace(temp, target)
                finally:
                    Path(temp).unlink(missing_ok=True)
                changed += 1
            else:
                atomic(target, payload)
                changed += 1
            if source and (safe_file(folder, rel) != source or stamp(source) != before):
                raise ValueError('Source changed during capture; rerun prepare')
            files[str(target)] = {**info, 'sha': sha, 'stamp': before}

        for entry in records:
            folder = self.root / 'entries' / entry['id']
            put(entry, 'description.md', payload=metadata(self.root, entry))
            for rel in sources(folder):
                if rel != 'notes.md':
                    put(entry, rel)
            if entry.get('tier', 'private') in {'gallery', 'portfolio'}:
                publish = entry.get('publish', {})
                selected = publish.get('files', [])
                if not isinstance(selected, list) or not all(isinstance(p, str) for p in selected):
                    raise ValueError('Invalid public file selection')
                if publish.get('notes') and 'notes.md' not in selected:
                    raise ValueError('Public notes must be selected')
                put(entry, 'description.md', public=True, payload=metadata(self.root, entry, public=True))
                for rel in selected:
                    source = safe_file(folder, rel)
                    if any(SECRET.search(part) or part in SKIP for part in Path(rel).parts):
                        raise ValueError('Excluded public file')
                    if not source.is_file():
                        raise ValueError('Selected public file is missing')
                    if source.suffix.lower().lstrip('.') in MEDIA | CODE:
                        put(entry, rel, public=True)
        # Prepare records desired work only. Indexed revisions survive until native replacement succeeds.
        state = {'version': 2, 'root': str(self.root), 'files': files,
                 'entries': {e['id']: e['_manifest'] for e in records}}
        state['generation'] = hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()
        save(self.manifest, state)
        return {'entries': len(records), 'files': len(files), 'changed': changed,
                'removed': len(known.keys() - files.keys()), 'generation': state['generation']}

    @staticmethod
    def logical(info):
        return info['lane'], info['entry'], info['source'], info['metadata']

    def snapshot_path(self, path, info):
        """Validate derived identities without following symlinks outside the corpus."""
        corpus = self.index / 'corpus'
        relative = Path(path).relative_to(corpus)
        expected = (info['lane'], info['entry'], 'metadata' if info['metadata'] else 'files', info['sha'])
        if tuple(relative.parts[:4]) != expected or not re.fullmatch(r'[0-9a-f]{64}', info['sha']):
            raise ValueError('Invalid content-addressed snapshot')
        return safe_file(corpus, relative)

    def provenance(self):
        """Read one desired/indexed pair per request; legacy data is private and explicitly stale."""
        desired = read(self.manifest, {})
        indexed = read(self.indexed, {})
        if indexed and indexed.get('root') != str(self.root):
            raise ValueError('Indexed provenance belongs to another collection')
        legacy = desired.get('version') != 2 or indexed.get('version') != 1
        files = indexed.get('files', desired.get('files', {}) if desired.get('version') == 1 else {})
        return desired, indexed, files, legacy

    def eligible_files(self, desired, indexed_files, legacy, current, eligible, scope, lanes):
        # Terminal skips attest processing, never retrieval evidence, even if old segments remain.
        skipped = set()
        for name in ('index.sqlite', 'code.sqlite'):
            database = self.index / name
            if database.exists():
                with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=5) as db:
                    skipped.update(row[0] for row in db.execute("SELECT path FROM files WHERE state='skipped'"))
        files, stale = {}, {}
        for path, info in indexed_files.items():
            ident = info['entry']
            if ident not in eligible or info['lane'] not in lanes or path in skipped:
                continue
            fresh = (not legacy and not info.get('legacy') and desired.get('files', {}).get(path) == info
                     and current[ident]['_manifest'] == info.get('manifest')
                     and not self.source_stale(info, current))
            if scope == 'public' and (not fresh or current[ident].get('tier') not in {'gallery', 'portfolio'}):
                continue
            files[path] = info
            stale[path] = not fresh
        return files, stale

    def reconcile(self):
        """Attest usable or terminal-skipped native paths; caller holds writer lock."""
        desired, previous, old_files, legacy = self.provenance()
        desired_keys = {self.logical(info) for info in desired.get('files', {}).values()}
        revisions = {self.logical(info): (path, info) for path, info in old_files.items()}
        # Keep the last usable private revision during failed refreshes. Revoked public sources need no fallback.
        revisions = {key: (path, info) for key, (path, info) in revisions.items()
                     if info['entry'] in desired.get('entries', {})
                     and (info['lane'] != 'public' or key in desired_keys)}
        processed = set()
        for name in ('index.sqlite', 'code.sqlite'):
            database = self.index / name
            if not database.exists():
                continue
            with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=5) as db:
                processed.update(row[0] for row in db.execute("""
                    SELECT f.path FROM files f WHERE f.state='skipped' OR (f.state IN ('done','partial') AND EXISTS (
                        SELECT 1 FROM segments s LEFT JOIN fts ON fts.rowid=s.id
                        WHERE s.file=f.id AND s.kind!='name'
                          AND (s.vec IS NOT NULL OR length(coalesce(fts.original,fts.body,''))>0)))
                    """))
        for path, info in desired.get('files', {}).items():
            if path not in processed:
                continue
            target = self.snapshot_path(path, info)
            if not target.is_file() or digest(target) != info['sha']:
                raise ValueError('Indexed snapshot no longer matches its content identity')
            revisions[self.logical(info)] = (path, info)
        files = dict(revisions.values())
        state = self.save_indexed(files)
        # Commit provenance before collection-owned garbage collection; never delete authored files or SQLite rows.
        keep = set(files) | set(desired.get('files', {}))
        corpus = self.index / 'corpus'
        for path in corpus.rglob('*'):
            if '.git' not in path.relative_to(corpus).parts and path.is_file() and str(path) not in keep:
                path.unlink()
        return state

    def save_indexed(self, files):
        state = {'version': 1, 'root': str(self.root), 'files': files}
        state['generation'] = hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()
        save(self.indexed, state)
        return state

    def command(self, operation):
        if not self.binary.is_file() or not os.access(self.binary, os.X_OK):
            raise ValueError('Build the DigUp fork first; see integrations/design-collection/README.md')
        return [str(self.binary), operation, '--index', str(self.index)]

    def counts(self, code=False):
        path = self.index / ('code.sqlite' if code else 'index.sqlite')
        if not path.exists():
            return {'files': 0, 'vectors': 0, 'pending': 0, 'failed': 0, 'unfinished': 0}
        with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5) as db:
            states = dict(db.execute('SELECT state,COUNT(*) FROM files GROUP BY state'))
            media = {'transcription': {}, 'captions': {}}
            for kind, raw in db.execute('SELECT kind,info FROM files'):
                try:
                    info = json.loads(raw or '{}')
                except (ValueError, TypeError):
                    info = {}
                if not isinstance(info, dict):
                    info = {}
                for category, key, allowed in (
                    ('transcription', 'transcript_status', {'transcribed', 'no_speech', 'no_audio', 'unavailable', 'failed', 'partial'}),
                    ('captions', 'captions_status', {'extracted', 'none', 'unavailable', 'failed', 'partial'}),
                ):
                    if kind not in {'audio', 'video'} and key not in info:
                        continue
                    value = info.get(key)
                    value = value if isinstance(value, str) and value in allowed else 'not_reported'
                    media[category][value] = media[category].get(value, 0) + 1
            return {'files': sum(states.values()), 'vectors': db.execute('SELECT COUNT(*) FROM segments WHERE vec IS NOT NULL').fetchone()[0],
                    'pending': states.get('pending', 0), 'failed': states.get('failed', 0),
                    'unfinished': states.get('partial', 0), 'states': states, 'media': media}

    def source_stale(self, info, current):
        """Compare a snapshot with its canonical source without discarding last-known results."""
        entry = current.get(info['entry'])
        if entry is None:
            return True
        try:
            if info['metadata']:
                return hashlib.sha256(metadata(self.root, entry, public=info['lane'] == 'public')).hexdigest() != info['sha']
            source = safe_file(self.root / 'entries' / info['entry'], info['source'])
            return not source.is_file() or stamp(source) != info['stamp']
        except (OSError, ValueError):
            return True

    def status(self):
        state, indexed, files, legacy = self.provenance()
        current = {e['id']: e for e in catalog(self.root)}
        counts = self.counts()
        receipt = read(self.index / 'receipt.json', {})
        code = self.counts(code=True)
        changed_sources = any(self.source_stale(info, current) for info in files.values())
        return {'engine': 'digup', 'ready': bool(counts['vectors']), **counts, 'code': code,
                'entries': len(current), 'generation': state.get('generation'),
                'indexedGeneration': indexed.get('generation'),
                'rebuildRequired': legacy or any(info.get('legacy') for info in files.values()),
                'stale': legacy or changed_sources or files != state.get('files', {})
                    or {ident: entry['_manifest'] for ident, entry in current.items()} != state.get('entries')
                    or receipt.get('generation') != state.get('generation'),
                'receipt': receipt, 'reason': 'Reindex to establish content-addressed provenance.' if legacy else
                    None if counts['vectors'] else 'Run the design collection index command.'}

    def index_all(self, limit=None):
        with self.writer(), interruptible():
            prepared = self.prepare()
            save(self.index / 'receipt.json', {'status': 'running', **prepared, 'startedAt': time.time()})
            roots = [self.index / 'corpus' / lane for lane in ('media', 'public')]
            for root in roots:
                root.mkdir(parents=True, exist_ok=True, mode=0o700)
            code = self.index / 'corpus/code'
            code.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                commands = [self.command('index-service') + [str(p) for p in roots] +
                            ['--include-datasets', '--include-code', '--code-root', str(code), '--backfill', '--prune',
                             '--memory-limit', '4096', '--time-limit', '180', '--linger', '0']]
                if limit is not None:
                    commands = [self.command('index') + [str(p) for p in roots] + ['--include-datasets', '--include-code', '--limit', str(limit)],
                                self.command('index') + [str(code), '--code', '--limit', str(limit)]]
                for command in commands:
                    for attempt in range(3):
                        child = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                                 text=True, start_new_session=True)
                        fatal = False
                        try:
                            for line in child.stdout:
                                print(line, end='', flush=True)
                                if command[1] == 'index-service':
                                    event = json.loads(line)
                                    fatal |= bool(event.get('fatal')) or event.get('event') == 'waiting'
                            exit_code = child.wait()
                        finally:
                            if child.poll() is None:
                                os.killpg(child.pid, signal.SIGTERM)
                                child.wait()
                            child.stdin.close()
                            child.stdout.close()
                        if exit_code == 75 and attempt < 2:
                            continue
                        if exit_code != 0 or fatal:
                            raise ValueError('DigUp indexing failed; completed files remain resumable')
                        break
                counts = self.counts()
                code_counts = self.counts(code=True)
                incomplete = any(part[key] for part in (counts, code_counts) for key in ('pending', 'failed', 'unfinished'))
                receipt = {'status': 'degraded' if incomplete else 'complete',
                           **prepared, **counts, 'code': code_counts, 'finishedAt': time.time()}
                indexed = self.reconcile()
                receipt['indexedGeneration'] = indexed['generation']
                save(self.index / 'text.json', {'generation': prepared['generation'], 'indexedGeneration': indexed['generation'],
                                                'entries': self.extracted_text()})
                save(self.index / 'receipt.json', receipt)
                return receipt
            except BaseException as error:
                failure = {'status': 'interrupted' if isinstance(error, KeyboardInterrupt) else 'failed',
                           **prepared, 'error': type(error).__name__, 'finishedAt': time.time()}
                try:
                    failure['indexedGeneration'] = self.reconcile()['generation']
                except Exception as reconcile_error:
                    # Preserve the native/startup error and report failed provenance reconciliation separately.
                    failure['provenanceError'] = type(reconcile_error).__name__
                save(self.index / 'receipt.json', failure)
                raise

    def close(self):
        with self.lock:
            child, self.child = self.child, None
            if child:
                child.terminate()
                try:
                    child.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
                child.stdin.close()
                child.stdout.close()

    def start(self):
        if self.child is not None and self.child.poll() is None:
            return
        self.close()
        self.lines = queue.Queue()
        command = self.command('search-service') + [str(self.index / 'corpus'), '--code-root', str(self.index / 'corpus/code'), '--encoder-idle', '60']
        self.child = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
        child, lines = self.child, self.lines

        def receive():
            failure = 'exited'
            try:
                for line in child.stdout:
                    value = json.loads(line)
                    if not isinstance(value, dict):
                        raise ValueError('Invalid helper response')
                    lines.put(value)
            except ValueError:
                failure = 'invalid response'
            except OSError:
                failure = 'read failure'
            finally:
                lines.put(ValueError('DigUp search helper ' + failure))
        threading.Thread(target=receive, daemon=True).start()
        ready = self.wait_for(None, ready=True)
        if 'path-scope-v1' not in ready.get('capabilities', []):
            self.close()
            raise ValueError('Use the design DigUp fork: this binary lacks pre-ranking path filters')
        self.generation = None

    def wait_for(self, ident, ready=False):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                value = self.lines.get(timeout=max(.001, deadline - time.monotonic()))
            except queue.Empty:
                break
            if isinstance(value, Exception):
                self.close()
                raise value
            if ready and value.get('event') == 'ready' or not ready and value.get('id') == ident:
                if value.get('error'):
                    raise ValueError(value['error'])
                return value
        self.close()
        raise ValueError('DigUp search timed out; the next request restarts the helper')

    def call(self, request):
        self.start()
        self.serial += 1
        request = {**request, 'id': self.serial}
        try:
            self.child.stdin.write(json.dumps(request) + '\n')
            self.child.stdin.flush()
        except OSError:
            self.close()
            raise ValueError('DigUp search helper write failure') from None
        return self.wait_for(self.serial)

    def search(self, records=None, query='', *, mode='hybrid', collection='', platform='', tag='', kind='', tier='', limit=30, scope='all', include_entries=False):
        if mode not in {'hybrid', 'semantic', 'keyword'} or scope not in {'all', 'public'}:
            raise ValueError('Invalid search mode or scope')
        if not isinstance(query, str) or len(query) > 500:
            raise ValueError('Use a query of at most 500 characters')
        with self.lock:
            state, indexed, indexed_files, legacy = self.provenance()
            # Intersect caller eligibility with current canonical visibility, not stale snapshot metadata.
            current = {e['id']: e for e in catalog(self.root)}
            requested = current.keys() if records is None else {e['id'] for e in records}
            eligible = {ident for ident in requested if ident in current
                        and (scope != 'public' or current[ident].get('tier', 'private') in {'gallery', 'portfolio'})
                        and (not collection or ('Other' if scope == 'public' else current[ident].get('collection') or 'Other') == collection)
                        and (not platform or platform in current[ident].get('platforms', []))
                        and (not tag or tag in current[ident].get('tags', []))
                        and (not kind or current[ident].get('kind') == kind)
                        and (not tier or current[ident].get('tier', 'private') == tier)}
            code = query.lstrip().lower().startswith('code:')
            if code:
                query = query.lstrip()[5:].strip()
            files, stale_sources = self.eligible_files(state, indexed_files, legacy, current, eligible, scope,
                {'public'} if scope == 'public' else {'code' if code else 'media'})
            if not query.strip() or not eligible:
                return {'engine': 'digup', 'mode': mode, 'results': []}
            if not files:
                return {'engine': 'digup', 'mode': 'keyword', 'results': [],
                        'notice': 'No native files are indexed for this selection.'}
            # The persistent helper observes newly committed files after each successful or interrupted index run.
            db = self.index / ('code.sqlite' if code else 'index.sqlite')
            wal = Path(str(db) + '-wal')
            generation = (indexed.get('generation'), state.get('generation'), db.stat().st_mtime_ns if db.exists() else 0,
                          wal.stat().st_mtime_ns if wal.exists() else 0)
            self.start()
            if self.generation != generation:
                self.call({'op': 'reload'})
                self.generation = generation
            reply = self.call({'op': 'search', 'q': query, 'code': code and scope != 'public', 'meaning': mode != 'keyword',
                               'keywords': mode != 'semantic', 'limit': max(1, len(files)), 'paths': list(files)})
            if mode == 'semantic' and not reply.get('meaning'):
                raise ValueError('DigUp query encoder is unavailable')
            results = {}
            for hit in reply.get('hits', []):
                info = files.get(hit.get('path'))
                if info is None:
                    continue
                ident = info['entry']
                segment = hit.get('segment')
                loc = {'kind': 'frame' if segment == 'frame' else segment, 'source': info['source']}
                if segment in {'audio', 'frame'}:
                    loc['seconds'] = hit.get('loc')
                    if hit.get('loc') is not None:
                        loc['start'] = hit['loc']
                    if hit.get('loc_end') is not None:
                        loc['end'] = hit['loc_end']
                elif segment in {'page', 'page_image'}:
                    loc['page'] = hit.get('loc')
                elif segment in {'lines', 'cells'}:
                    loc.update(start=hit.get('focus') or hit.get('loc'), end=hit.get('loc_end'))
                path = hit['path']
                evidence = {'content': (hit.get('excerpt') or '')[:400], 'location': loc,
                            'similarity': hit.get('cosine'), 'score': hit['score'], 'stale': stale_sources[path]}
                lane = 'image' if segment in {'image', 'frame', 'page_image'} else 'audio' if segment == 'audio' else 'text'
                result = results.setdefault(ident, {'id': ident, 'score': hit['score'], 'matches': {}})
                result['matches'].setdefault(lane, evidence)
            ranked = list(results.values())[:limit]
            stale = any(evidence['stale'] for result in ranked for evidence in result['matches'].values())
            for result in ranked:
                result['stale'] = any(evidence['stale'] for evidence in result['matches'].values())
                if result['stale']:
                    result['notice'] = 'Some matches use outdated source snapshots; reindex to refresh.'
            if include_entries:
                for hit in ranked:
                    keys = ('id', 'title', 'summary', 'kind', 'tier', 'platforms', 'tags') if scope == 'public' else (
                        'id', 'title', 'summary', 'kind', 'tier', 'collection', 'platforms', 'tags', 'reuse', 'reuseEntry', 'reuseRepository')
                    hit['entry'] = {key: current[hit['id']].get(key) for key in keys}
            notice = reply.get('warning') or (None if reply.get('meaning') or mode == 'keyword' else 'DigUp meaning search is unavailable; showing keyword matches.')
            return {'engine': 'digup', 'mode': ('semantic' if mode == 'semantic' else 'hybrid') if reply.get('meaning') else 'keyword',
                    'results': ranked, 'ms': reply.get('ms'), 'stale': stale, 'notice': notice}

    def extracted_text(self, scope='all', max_chars=180_000):
        """Content-only BM25 projection from the same readers; no vectors or local paths."""
        if scope not in {'all', 'public'}:
            raise ValueError('Invalid scope')
        state, indexed, indexed_files, legacy = self.provenance()
        current = {e['id']: e for e in catalog(self.root)}
        files, _ = self.eligible_files(state, indexed_files, legacy, current, current.keys(), scope,
                                      {'public'} if scope == 'public' else {'media', 'code'})
        result, seen = {}, {}
        for name in ('index.sqlite', 'code.sqlite') if scope == 'all' else ('index.sqlite',):
            path = self.index / name
            if not path.exists():
                continue
            with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5) as db:
                for file, text in db.execute('SELECT f.path,coalesce(fts.original,fts.body) FROM files f JOIN segments s ON s.file=f.id JOIN fts ON fts.rowid=s.id WHERE s.kind != ? ORDER BY f.path,s.loc,s.id', ('name',)):
                    info = files.get(file)
                    if info is None or not text:
                        continue
                    ident = info['entry']
                    if text in seen.setdefault(ident, set()) or len(result.get(ident, '')) >= max_chars:
                        continue
                    seen[ident].add(text)
                    result[ident] = (result.get(ident, '') + '\n' + text)[:max_chars]
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=os.environ.get('DESIGN_LIBRARY_ROOT'), required=not os.environ.get('DESIGN_LIBRARY_ROOT'))
    parser.add_argument('--index', type=Path)
    parser.add_argument('--digup', type=Path)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('prepare')
    command = commands.add_parser('index')
    command.add_argument('--limit', type=int)
    commands.add_parser('status')
    command = commands.add_parser('search')
    command.add_argument('query')
    command.add_argument('--collection', default='')
    command.add_argument('--tag', default='')
    command.add_argument('--scope', choices=['all', 'public'], default='all')
    command.add_argument('--mode', choices=['hybrid', 'semantic', 'keyword'], default='hybrid')
    commands.add_parser('serve')
    args = parser.parse_args()
    os.umask(0o077)
    library = Collection(args.root, args.index, args.digup)
    try:
        if args.command == 'prepare':
            with library.writer():
                result = library.prepare()
        elif args.command == 'index':
            result = library.index_all(args.limit)
        elif args.command == 'status':
            result = library.status()
        elif args.command == 'search':
            result = library.search(query=args.query, mode=args.mode, scope=args.scope,
                                    collection=args.collection, tag=args.tag)
        else:
            for line in sys.stdin:
                request = {}
                try:
                    request = json.loads(line)
                    if not isinstance(request, dict):
                        raise ValueError('Expected a request object')
                    if request.get('op') == 'status':
                        result = library.status()
                    elif request.get('op', 'search') == 'search':
                        fields = {key: request.get(key, '') for key in ('collection', 'platform', 'tag', 'kind', 'tier')}
                        if any(not isinstance(v, str) for v in fields.values()):
                            raise ValueError('Invalid filter')
                        result = library.search(query=request.get('q', ''), **fields,
                                                scope=request.get('scope', 'all'), limit=200)
                    else:
                        raise ValueError('Unknown operation')
                    print(json.dumps({'id': request.get('id'), **result}), flush=True)
                except Exception as error:
                    print(json.dumps({'id': request.get('id') if isinstance(request, dict) else None,
                                      'error': str(error)[:300]}), flush=True)
            return 0
        print(json.dumps(result, indent=2))
        return 2 if result.get('status') == 'degraded' else 0
    finally:
        library.close()


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
