"""Adapter contracts; temporary authored collections, no models or native extraction."""
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest


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


if __name__ == '__main__':
    unittest.main()
