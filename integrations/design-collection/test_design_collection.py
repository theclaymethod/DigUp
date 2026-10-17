"""Adapter contracts; temporary authored collections, no models or native extraction."""
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location('design_collection', Path(__file__).with_name('design_collection.py'))
adapter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)


class MediaEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.entry = self.root / 'entries/demo'
        (self.entry / 'assets').mkdir(parents=True)
        self.manifest = {'title': 'Demo', 'tier': 'gallery', 'publish': {
            'files': ['assets/clip.mp4', 'assets/selected.vtt']}}
        adapter.save(self.entry / 'entry.json', self.manifest)
        (self.entry / 'notes.md').write_text('Private notes before refresh')
        (self.entry / 'assets/clip.mp4').write_bytes(b'placeholder; never decoded')
        (self.entry / 'assets/selected.vtt').write_text('WEBVTT\n\n00:00:04.000 --> 00:00:11.000\nSelected caption\n')
        (self.entry / 'assets/private.srt').write_text('1\n00:00:04,000 --> 00:00:11,000\nPrivate caption\n')
        self.collection = adapter.Collection(self.root, binary=sys.executable)
        self.addCleanup(self.collection.close)
        self.collection.prepare()
        self.state = adapter.read(self.collection.manifest)
        # Controlled native replies below represent an already indexed revision, not prepare alone.
        adapter.save(self.collection.index / 'indexed.json', {
            'version': 1, 'root': str(self.collection.root), 'generation': self.state['generation'],
            'files': {path: {**info, 'manifest': self.state['entries'][info['entry']]}
                      for path, info in self.state['files'].items()},
        })
        self.requests = []
        self.hits = []
        self.collection.start = lambda: None

        def call(request):
            self.requests.append(request)
            return {'hits': self.hits, 'meaning': False}

        self.collection.call = call

    def path(self, source='assets/clip.mp4', lane='media'):
        return next(path for path, info in self.state['files'].items()
                    if info['source'] == source and info['lane'] == lane and not info['metadata'])

    def hit(self, **changes):
        return {'path': self.path(), 'segment': 'audio', 'modality': 'text',
                'loc': 4.0, 'loc_end': 11.0, 'excerpt': 'Spoken words', 'score': 0.8, **changes}

    def search(self, **kwargs):
        return self.collection.search(query='spoken', mode='keyword', **kwargs)

    def database(self):
        db = sqlite3.connect(self.collection.index / 'index.sqlite')
        self.addCleanup(db.close)
        db.executescript('''
            CREATE TABLE files(id INTEGER PRIMARY KEY, path TEXT, kind TEXT, state TEXT, info TEXT);
            CREATE TABLE segments(id INTEGER PRIMARY KEY, file INTEGER, kind TEXT, loc REAL, vec BLOB);
            CREATE TABLE fts(body TEXT, original TEXT);
        ''')
        return db

    def test_transcript_is_timed_audio_evidence(self):
        self.hits = [self.hit()]
        result = self.search()
        match = result['results'][0]['matches']['audio']
        self.assertEqual(match['content'], 'Spoken words')
        self.assertEqual(match['location'], {'kind': 'audio', 'source': 'assets/clip.mp4',
                                           'seconds': 4.0, 'start': 4.0, 'end': 11.0})
        self.assertFalse(match['stale'])
        self.assertFalse(result['stale'])
        self.assertIsNone(result['notice'])

    def test_null_audio_excerpt_and_frame_ranges(self):
        self.hits = [self.hit(excerpt=None), self.hit(segment='frame', excerpt=None)]
        matches = self.search()['results'][0]['matches']
        self.assertEqual(matches['audio']['content'], '')
        self.assertEqual(matches['image']['location']['end'], 11.0)

    def test_changed_missing_or_symlink_source_retains_flagged_snapshot(self):
        source = self.entry / 'assets/clip.mp4'
        self.hits = [self.hit()]
        source.write_bytes(b'changed source')
        for state in ('changed', 'missing', 'symlink'):
            with self.subTest(state=state):
                if state == 'missing':
                    source.unlink()
                elif state == 'symlink':
                    source.symlink_to(self.entry / 'assets/selected.vtt')
                result = self.search()
                hit = result['results'][0]
                self.assertTrue(result['stale'])
                self.assertTrue(hit['stale'])
                self.assertTrue(hit['matches']['audio']['stale'])
                self.assertIn('outdated source snapshots', hit['notice'])
                self.assertIsNone(result['notice'])  # global notice remains availability-only

    def test_metadata_notes_change_is_stale(self):
        path = next(path for path, info in self.state['files'].items()
                    if info['metadata'] and info['lane'] == 'media')
        self.hits = [self.hit(path=path, segment='chunk')]
        (self.entry / 'notes.md').write_text('Updated private notes')
        self.assertTrue(self.search()['results'][0]['stale'])

    def test_status_and_search_agree_about_changed_sources(self):
        adapter.save(self.collection.index / 'receipt.json', {'generation': self.state['generation']})
        self.assertFalse(self.collection.status()['stale'])
        self.hits = [self.hit()]
        source = self.entry / 'assets/clip.mp4'
        source.unlink()
        source.symlink_to(self.entry / 'assets/selected.vtt')
        self.assertTrue(self.collection.status()['stale'])
        self.assertTrue(self.search()['results'][0]['stale'])

    def test_public_caption_selection_and_manifest_revocation(self):
        private = self.path('assets/private.srt')
        public = self.path('assets/selected.vtt', 'public')
        self.hits = [self.hit(path=private), self.hit(path=public)]
        result = self.search(scope='public')
        self.assertEqual(result['results'][0]['matches']['audio']['location']['source'], 'assets/selected.vtt')
        paths = self.requests[-1]['paths']
        self.assertIn(public, paths)
        self.assertNotIn(private, paths)
        self.manifest['publish']['files'] = ['assets/clip.mp4']
        adapter.save(self.entry / 'entry.json', self.manifest)
        self.assertEqual(self.search(scope='public')['results'], [])

    def test_transcripts_project_to_bm25_with_exact_public_files(self):
        db = self.database()
        for ident, (path, text) in enumerate(((self.path(), 'Private transcript'),
                                             (self.path('assets/selected.vtt', 'public'), 'Selected transcript')), 1):
            db.execute('INSERT INTO files VALUES(?,?,?,?,?)', (ident, path, 'video', 'done', '{}'))
            db.execute('INSERT INTO segments VALUES(?,?,?,?,?)', (ident, ident, 'audio', 4, None))
            db.execute('INSERT INTO fts(rowid,body,original) VALUES(?,?,?)', (ident, text, text))
        db.commit()
        self.assertIn('Private transcript', self.collection.extracted_text()['demo'])
        public = self.collection.extracted_text(scope='public')['demo']
        self.assertIn('Selected transcript', public)
        self.assertNotIn('Private transcript', public)
        self.manifest['publish']['files'] = []
        adapter.save(self.entry / 'entry.json', self.manifest)
        self.assertEqual(self.collection.extracted_text(scope='public'), {})

    def test_public_text_rejects_stale_selected_source_but_private_keeps_snapshot(self):
        db = self.database()
        for ident, lane in enumerate(('media', 'public'), 1):
            db.execute('INSERT INTO files VALUES(?,?,?,?,?)', (ident, self.path(lane=lane), 'video', 'done', '{}'))
            db.execute('INSERT INTO segments VALUES(?,?,?,?,?)', (ident, ident, 'audio', 4, None))
            db.execute('INSERT INTO fts(rowid,body,original) VALUES(?,?,?)',
                       (ident, 'Previously spoken content', 'Previously spoken content'))
        db.commit()
        self.assertIn('Previously spoken content', self.collection.extracted_text(scope='public')['demo'])
        source = self.entry / 'assets/clip.mp4'
        source.write_bytes(b'new canonical audio content')
        for state in ('changed', 'missing', 'symlink'):
            with self.subTest(state=state):
                if state == 'missing':
                    source.unlink()
                elif state == 'symlink':
                    source.symlink_to(self.entry / 'assets/selected.vtt')
                self.assertEqual(self.collection.extracted_text(scope='public'), {})
                self.assertIn('Previously spoken content', self.collection.extracted_text()['demo'])

    def test_counts_report_native_outcomes_without_claiming_old_index_transcripts(self):
        db = self.database()
        for ident, info in enumerate(({'transcript_status': 'transcribed', 'captions_status': 'none'},
                                     {'transcript_status': 'unavailable', 'captions_status': 'extracted'},
                                     {}, {'transcript_status': 'arbitrary untrusted detail'}), 1):
            db.execute('INSERT INTO files VALUES(?,?,?,?,?)', (ident, str(ident), 'video',
                                                              'partial' if ident == 2 else 'done', json.dumps(info)))
        db.commit()
        counts = self.collection.counts()
        self.assertEqual(counts['unfinished'], 1)
        self.assertEqual(counts['media']['transcription'], {'transcribed': 1, 'unavailable': 1, 'not_reported': 2})
        self.assertEqual(counts['media']['captions'], {'none': 1, 'extracted': 1, 'not_reported': 2})

    def test_prepare_cannot_bless_old_native_text_after_equal_size_edit(self):
        db = self.database()
        for ident, lane in enumerate(('media', 'public'), 1):
            db.execute('INSERT INTO files VALUES(?,?,?,?,?)', (ident, self.path(lane=lane), 'video', 'done', '{}'))
            db.execute('INSERT INTO segments VALUES(?,?,?,?,?)', (ident, ident, 'audio', 4, None))
            db.execute('INSERT INTO fts(rowid,body,original) VALUES(?,?,?)', (ident, 'Old revision words', 'Old revision words'))
        db.commit()
        self.assertIn('Old revision words', self.collection.extracted_text(scope='public')['demo'])
        old_path = self.path()
        source = self.entry / 'assets/clip.mp4'
        before = source.stat()
        source.write_bytes(b'x' * before.st_size)
        os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.collection.prepare()
        self.hits = [self.hit(path=old_path)]
        self.assertEqual(self.collection.extracted_text(scope='public'), {})
        self.assertEqual(self.search(scope='public')['results'], [])
        self.assertIn('Old revision words', self.collection.extracted_text()['demo'])
        self.assertTrue(self.search()['results'][0]['stale'])

    def test_immutable_capture_reuses_bytes_and_preserves_pending_and_indexed_revisions(self):
        old_path = self.path()
        indexed = (self.collection.index / 'indexed.json').read_bytes()
        self.assertEqual(self.collection.prepare()['changed'], 0)
        self.assertEqual(adapter.read(self.collection.manifest), self.state)
        self.assertEqual(Path(old_path).parent.parent.name, self.state['files'][old_path]['sha'])
        source = self.entry / 'assets/clip.mp4'
        before = source.stat()
        source.write_bytes(b'x' * before.st_size)
        os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.collection.prepare()
        next_state = adapter.read(self.collection.manifest)
        new_path = next(path for path, info in next_state['files'].items()
                        if self.collection.logical(info) == self.collection.logical(self.state['files'][old_path]))
        self.assertNotEqual(old_path, new_path)
        self.assertTrue(Path(old_path).is_file())
        self.assertTrue(Path(new_path).is_file())
        self.assertEqual((self.collection.index / 'indexed.json').read_bytes(), indexed)
        self.assertEqual(self.collection.prepare()['changed'], 0)

    def test_capture_race_does_not_advance_desired_or_indexed_state(self):
        desired = self.collection.manifest.read_bytes()
        indexed = self.collection.indexed.read_bytes()
        source = self.entry / 'assets/clip.mp4'
        source.write_bytes(b'changed content needing capture')
        copy = adapter.shutil.copy2
        # Mutate after the copy, before the source's post-capture stamp is read.
        with patch.object(adapter.shutil, 'copy2', side_effect=lambda *args: (
                copy(*args), source.write_bytes(b'edited during capture'))[0]) as copied:
            with self.assertRaisesRegex(ValueError, 'changed during capture'):
                self.collection.prepare()
            self.assertTrue(copied.called)
        self.assertEqual(self.collection.manifest.read_bytes(), desired)
        self.assertEqual(self.collection.indexed.read_bytes(), indexed)

    def test_corrupt_or_symlink_snapshot_is_rejected_before_native_work(self):
        target = Path(self.path())
        target.write_bytes(b'corrupt snapshot')
        with self.assertRaisesRegex(ValueError, 'Corrupt content-addressed snapshot'):
            self.collection.prepare()
        target.unlink()
        target.symlink_to(self.entry / 'assets/clip.mp4')
        with self.assertRaisesRegex(ValueError, 'Symlink'):
            self.collection.prepare()

    def test_missing_binary_and_interruption_leave_terminal_receipts_and_old_private_snapshot(self):
        self.hits = [self.hit()]
        (self.entry / 'assets/clip.mp4').write_bytes(b'new revision')
        self.collection.binary = self.root / 'missing-binary'
        with self.assertRaisesRegex(ValueError, 'Build the DigUp fork'):
            self.collection.index_all()
        self.assertEqual(adapter.read(self.collection.index / 'receipt.json')['status'], 'failed')
        self.assertTrue(self.search()['results'][0]['stale'])
        self.assertEqual(self.search(scope='public')['results'], [])
        with patch.object(self.collection, 'command', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.collection.index_all()
        self.assertEqual(adapter.read(self.collection.index / 'receipt.json')['status'], 'interrupted')
        self.assertTrue(self.search()['results'][0]['stale'])
        restarted = adapter.Collection(self.root, binary=sys.executable)
        self.addCleanup(restarted.close)
        self.assertTrue(restarted.status()['stale'])
        self.assertIn(self.path(), adapter.read(restarted.indexed)['files'])

    def test_reconcile_requires_usable_native_content_and_keeps_partial_work(self):
        db = self.database()
        old_path = self.path()
        old_public = self.path(lane='public')
        (self.entry / 'assets/clip.mp4').write_bytes(b'replacement bytes')
        self.collection.prepare()
        desired = adapter.read(self.collection.manifest)
        paths = {info['lane']: path for path, info in desired['files'].items()
                 if info['source'] == 'assets/clip.mp4'}
        for ident, lane in enumerate(('media', 'public'), 1):
            db.execute('INSERT INTO files VALUES(?,?,?,?,?)', (ident, paths[lane], 'video', 'pending', '{}'))
            db.execute('INSERT INTO segments VALUES(?,?,?,?,?)', (ident, ident, 'name', 4, None))
            db.execute('INSERT INTO fts(rowid,body,original) VALUES(?,?,?)', (ident, 'replacement', 'replacement'))
        db.commit()
        for state, kind in (('pending', 'audio'), ('failed', 'audio'), ('done', 'name')):
            with self.subTest(state=state, kind=kind):
                db.execute('UPDATE files SET state=?', (state,))
                db.execute('UPDATE segments SET kind=?', (kind,))
                db.commit()
                with self.collection.writer():
                    self.collection.reconcile()
                files = adapter.read(self.collection.indexed)['files']
                self.assertIn(old_path, files)
                self.assertNotIn(paths['media'], files)
                self.assertEqual(self.collection.extracted_text(scope='public'), {})
        db.execute("UPDATE files SET state='partial'")
        db.execute("UPDATE segments SET kind='audio'")
        db.commit()
        # Reconcile completed per-file work even when command construction is interrupted.
        with patch.object(self.collection, 'command', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.collection.index_all()
        files = adapter.read(self.collection.indexed)['files']
        self.assertIn(paths['media'], files)
        self.assertNotIn(old_path, files)
        self.assertNotIn(old_public, files)
        self.assertFalse(Path(old_path).exists())
        self.assertTrue(Path(paths['media']).exists())
        self.assertEqual(self.collection.extracted_text(scope='public'), {'demo': '\nreplacement'})
        self.hits = [self.hit(path=paths['media'])]
        self.assertFalse(self.search()['results'][0]['stale'])

    def test_legacy_map_is_private_stale_until_real_replacement_is_attested(self):
        self.collection.indexed.unlink()
        legacy = {**self.state, 'version': 1}
        # Use a real legacy stable path to exercise migration retention and safe corpus GC.
        original = self.path()
        old_path = str(self.collection.index / 'corpus/media/demo/files/assets/clip.mp4')
        Path(old_path).parent.mkdir(parents=True)
        Path(old_path).write_bytes(Path(original).read_bytes())
        legacy['files'] = {old_path: {key: value for key, value in self.state['files'][original].items()
                                    if key != 'manifest'}}
        adapter.save(self.collection.manifest, legacy)
        self.hits = [self.hit(path=old_path)]
        self.assertTrue(self.collection.status()['rebuildRequired'])
        self.assertTrue(self.search()['results'][0]['stale'])
        self.assertEqual(self.search(scope='public')['results'], [])
        self.collection.prepare()
        self.assertTrue(Path(old_path).exists())
        self.assertTrue(self.search()['results'][0]['stale'])
        with self.collection.writer():
            self.collection.reconcile()
        self.assertTrue(Path(old_path).exists())
        self.assertTrue(self.collection.status()['rebuildRequired'])

    def test_changed_notes_policy_cannot_bless_old_metadata_after_prepare(self):
        self.manifest['publish'].update(notes=True, files=['notes.md'])
        adapter.save(self.entry / 'entry.json', self.manifest)
        self.collection.prepare()
        state = adapter.read(self.collection.manifest)
        adapter.save(self.collection.indexed, {'version': 1, 'root': str(self.collection.root),
                                             'files': state['files']})
        db = self.database()
        path = next(path for path, info in state['files'].items() if info['lane'] == 'public' and info['metadata'])
        db.execute('INSERT INTO files VALUES(?,?,?,?,?)', (1, path, 'doc', 'done', '{}'))
        db.execute('INSERT INTO segments VALUES(?,?,?,?,?)', (1, 1, 'chunk', 0, None))
        db.execute('INSERT INTO fts(rowid,body,original) VALUES(?,?,?)', (1, 'Private notes before refresh', None))
        db.commit()
        self.assertIn('Private notes', self.collection.extracted_text(scope='public')['demo'])
        self.manifest['publish'] = {'files': [], 'notes': False}
        adapter.save(self.entry / 'entry.json', self.manifest)
        self.collection.prepare()
        self.hits = [self.hit(path=path, segment='chunk')]
        self.assertEqual(self.search(scope='public')['results'], [])
        self.assertEqual(self.collection.extracted_text(scope='public'), {})

    def test_reconciliation_collects_abandoned_prepares_but_keeps_latest_desired_and_indexed(self):
        indexed = adapter.read(self.collection.indexed)
        source = self.entry / 'assets/clip.mp4'
        captured = set()
        for content in (b'revision B', b'revision C', b'revision D'):
            source.write_bytes(content)
            self.collection.prepare()
            captured.update(adapter.read(self.collection.manifest)['files'])
        desired = adapter.read(self.collection.manifest)
        retired = captured - set(desired['files']) - set(indexed['files'])
        self.assertTrue(retired)
        self.assertTrue(all(Path(path).is_file() for path in retired))
        outside = self.root / 'authored-unrelated.txt'
        outside.write_text('Must survive derived GC')
        with self.collection.writer():
            self.collection.reconcile()
        self.assertTrue(all(not Path(path).exists() for path in retired))
        self.assertTrue(all(Path(path).is_file() for path in set(indexed['files']) | set(desired['files'])))
        self.assertEqual(outside.read_text(), 'Must survive derived GC')
        self.assertEqual(source.read_bytes(), b'revision D')


if __name__ == '__main__':
    unittest.main()
