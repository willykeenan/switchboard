import concurrent.futures
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from room_reader import RoomStore
from room_updates import update


class RoomReaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.room = self.root/'global'
        self.room.mkdir()
        self.log = self.room/'messages.jsonl'
        self.log.write_text('')
        (self.room/'room.json').write_text('{"title":"Shared room"}')
        self.state = self.root/'board'/'subscription.json'
        self.store = RoomStore(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def append(self, seq, body='A full original message', author='codex', kind='result'):
        m = {'seq':seq,'id':str(seq),'author':author,'kind':kind,'body':body,
             'recipients':['operator'],'createdAt':'2026-09-07T03:00:00Z','replyTo':None}
        with self.log.open('a') as f:
            f.write(json.dumps(m)+'\n')
        return m

    def test_full_history_search_paging_and_exact_message(self):
        for i in range(1,76):
            self.append(i, 'Original NEEDLE' if i==3 else 'Ordinary message', 'claude' if i%2 else 'codex')
        newest = self.store.query('global')
        self.assertEqual(list(range(75,45,-1)), [m['seq'] for m in newest['messages']])
        older = self.store.query('global',before=newest['nextBefore'])
        self.assertEqual(45,older['messages'][0]['seq'])
        self.assertEqual(3,self.store.query('global',q='needle')['messages'][0]['seq'])
        self.assertEqual(37,self.store.query('global',author='codex')['matched'])
        self.assertEqual(1,self.store.query('global',message=2)['matched'])

    def test_cache_observes_append_and_replacement(self):
        self.append(1)
        self.assertEqual(1,self.store.query('global')['total'])
        self.append(2)
        self.assertEqual(2,self.store.query('global')['total'])
        replacement=self.room/'new.log'
        replacement.write_text(json.dumps({'seq':8,'author':'claude','body':'Replaced'})+'\n')
        replacement.replace(self.log)
        self.assertEqual(8,self.store.query('global')['latestSeq'])

    def test_partial_and_corrupt_rows_are_visible_without_losing_valid_rows(self):
        self.append(1)
        with self.log.open('a') as f:
            f.write('bad json\n{"seq":2,"author":"claude","body":"par')
        page=self.store.query('global')
        self.assertEqual(1,page['total']);self.assertEqual(1,page['invalidLines']);self.assertTrue(page['partialTail'])
        with self.log.open('a') as f:f.write('tial"}\n')
        page=self.store.query('global')
        self.assertEqual(2,page['total']);self.assertFalse(page['partialTail'])

    def test_traversal_and_symlinks_are_rejected(self):
        for name in ['../global','/global','global/..','..','%2e%2e']:
            with self.assertRaises(ValueError):self.store.query(name)
        (self.root/'alias').symlink_to(self.room,target_is_directory=True)
        self.assertNotIn('alias',[r['name'] for r in self.store.catalog()['rooms']])
        with self.assertRaises(OSError):self.store.query('alias')
        outside=self.root/'secret';outside.write_text('secret')
        self.log.unlink();self.log.symlink_to(outside)
        with self.assertRaises(OSError):self.store.query('global')

    def test_readers_preserve_source_bytes_and_do_not_create_agent_cursors(self):
        body='<script>alert(1)</script>\n'+('long original 雪 '*1500)
        m=self.append(1,body)
        before={str(p.relative_to(self.root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in self.root.rglob('*') if p.is_file()}
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            pages=list(pool.map(lambda _:self.store.query('global'),range(18)))
        self.assertTrue(all(p['messages']==[m] for p in pages))
        self.store.catalog()
        after={str(p.relative_to(self.root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in self.root.rglob('*') if p.is_file()}
        self.assertEqual(before,after)

    def test_bad_limits_and_duplicate_sequences(self):
        self.append(1);self.append(1)
        self.assertEqual(1,self.store.query('global')['invalidLines'])
        for limit in [0,101,-1]:
            with self.assertRaises(ValueError):self.store.query('global',limit=limit)

    def test_chat_batch_retries_until_exact_ack_and_never_skips_new_arrivals(self):
        self.append(1)
        self.assertTrue(update(self.root,self.state,'init')['initialized'])
        self.assertIsNone(update(self.root,self.state,'prepare')['batchId'])
        self.append(2, 'New message')
        batch=update(self.root,self.state,'prepare')
        self.assertEqual([2],[m['seq'] for m in batch['messages']])
        self.append(3,'Later arrival')
        self.assertEqual(batch,update(self.root,self.state,'prepare'))
        with self.assertRaises(ValueError):update(self.root,self.state,'ack','wrong-batch')
        update(self.root,self.state,'ack',batch['batchId'])
        self.assertEqual([3],[m['seq'] for m in update(self.root,self.state,'prepare')['messages']])
        self.assertFalse(update(self.root,self.state,'init')['initialized'])

    def test_chat_new_rooms_and_unreadable_history(self):
        self.append(1);update(self.root,self.state,'init')
        another=self.root/'new-room';another.mkdir()
        (another/'messages.jsonl').write_text(json.dumps({'seq':1,'author':'claude','body':'New room'})+'\n')
        with self.log.open('a') as f:f.write('invalid\n')
        batch=update(self.root,self.state,'prepare')
        self.assertEqual('new-room',batch['messages'][0]['sourceRoom'])
        self.assertEqual('global',batch['errors'][0]['room'])
        self.assertNotIn('global',batch['through'])
        self.assertIn('room=new-room',batch['messages'][0]['readerUrl'])


if __name__ == '__main__':unittest.main()
