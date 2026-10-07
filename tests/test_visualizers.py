import tempfile
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch
from labradour.visualizers import BuiltinRegistry,EvidenceReader,VisualizerJob,LIMIT
from labradour.snapshots import ScratchHistory

class VisualizerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve();self.history=ScratchHistory(self.root)
        self.records=[]
    def snapshot(self,manifest,changes=(),metadata=(),issues=()):
        before=self.history.head;commit=self.history.checkpoint(manifest,'fixture')
        row=dict(sequence=len(self.records)+1,event_id=str(len(self.records)),kind='snapshot.completed',session_id='host',
            payload=dict(commit=commit,before_commit=before,changes=list(changes),metadata=list(metadata),issues=list(issues)))
        self.records.append(row);return row
    def effect(self,snapshot,path,operation):
        return dict(id='effect',snapshot_event_id=snapshot['event_id'],path=path,operation=operation,
            checkpoint=snapshot['payload']['commit'],before_commit=snapshot['payload']['before_commit'],
            quality='overlapping-calls',capture_quality='observed-live-state',attribution='external-or-unknown',candidate_action_ids=['a','b'])
    def prepare(self,effect):
        return BuiltinRegistry().prepare(self.root,self.records,dict(kind='effect',payload=effect))
    def test_create_diff_delete_use_saved_bytes_after_live_file_changes(self):
        self.snapshot({})
        created=self.snapshot({'x.py':('100644',b'alpha\n')},[dict(path='x.py',operation='file.create')])
        changed=self.snapshot({'x.py':('100755',b'beta\n')},[dict(path='x.py',operation='file.modify')])
        removed=self.snapshot({},[dict(path='x.py',operation='file.delete')])
        (self.root/'x.py').write_text('LIVE MUST NEVER APPEAR')
        self.assertIn('alpha','\n'.join(self.prepare(self.effect(created,'x.py','file.create'))[1]))
        title,lines=self.prepare(self.effect(changed,'x.py','file.modify'))
        text='\n'.join(lines);self.assertIn('Source diff',title);self.assertIn('-alpha',text);self.assertIn('+beta',text)
        self.assertNotIn('LIVE MUST',text);self.assertIn('overlapping-calls',text)
        title,lines=self.prepare(self.effect(removed,'x.py','file.delete'))
        self.assertEqual(title,'File deleted');self.assertIn('beta',lines)
    def test_empty_binary_symlink_non_utf8_and_limits_are_distinct(self):
        self.snapshot({})
        for path,mode,data in [('empty','100644',b''),('binary','100644',b'\0x'),('link','120000',b'/outside/secret'),
                               ('invalid','100644',b'\xff'),('large','100644',b'x'*(LIMIT+1))]:
            snap=self.snapshot({path:(mode,data)},[dict(path=path,operation='file.create')])
            _,lines=self.prepare(self.effect(snap,path,'file.create'))
            text='\n'.join(lines)
            if path=='empty':self.assertIn('[empty file]',text)
            if path in ('binary','invalid'):self.assertIn('binary/non-UTF-8',text)
            if path=='link':self.assertIn('destination is never followed',text)
            if path=='large':self.assertIn('metadata_only',text);self.assertIn('visualizer read limit',text)
    def test_omission_missing_baseline_stale_and_bad_grants(self):
        before=self.snapshot({'x':('100644',b'old')})
        omitted=self.snapshot({},[dict(path='x',operation='file.omitted')],metadata=[dict(path='x',type='metadata-only',reason='policy.metadata_only')])
        effect=self.effect(omitted,'x','file.omitted')
        _,lines=self.prepare(effect);self.assertIn('policy.metadata_only','\n'.join(lines))
        self.assertNotIn('File deleted',lines)
        with self.assertRaises(ValueError):EvidenceReader(self.root,self.records).effect(dict(effect,path='../escape'))
        with self.assertRaises(ValueError):EvidenceReader(self.root,self.records).effect(dict(effect,checkpoint='0'*40))
        stale=self.snapshot({'x':('100644',b'old')},issues=[dict(path='x',error='read failed')])
        self.assertTrue(EvidenceReader(self.root,self.records).describe(stale,'x')['stale'])
        self.assertEqual(EvidenceReader(self.root,self.records).describe(None,'x')['state'],'unavailable')
        with self.assertRaises(ValueError):EvidenceReader(self.root,self.records).describe(before,'../x')
    def test_unusual_byte_paths_and_missing_git_objects(self):
        self.snapshot({})
        path='weird\n\t'+bytes([255]).decode('utf-8','surrogateescape')
        snap=self.snapshot({path:('100644',b'saved')},[dict(path=path,operation='file.create')])
        after=EvidenceReader(self.root,self.records).effect(self.effect(snap,path,'file.create'))[1]
        self.assertEqual(after['bytes'],b'saved')
        with patch.object(EvidenceReader,'git',side_effect=ValueError('missing')):
            self.assertEqual(EvidenceReader(self.root,self.records).describe(snap,path)['state'],'unavailable')
    def test_command_opaque_missing_and_empty_outputs_and_generic_fallback(self):
        action=dict(tool='Bash',state='completed',result_outcomes=['unknown'],correlation_quality='incomplete-evidence',
                    identity_quality='explicit-call-scope',notes=['missing boundary'],observations=['tool'])
        row=dict(kind='action',payload=action,children=[])
        records=[dict(event_id='tool',kind='adapter.event',payload=dict(payload=dict(raw={'tool_input':{'command':'DO NOT EXECUTE'},'tool_response':''})))]
        title,lines=BuiltinRegistry().prepare(self.root,records,row)
        self.assertIn('Command',title);self.assertIn('DO NOT EXECUTE','\n'.join(lines));self.assertNotIn('Output unavailable','\n'.join(lines))
        del records[0]['payload']['payload']['raw']['tool_response']
        self.assertIn('Output unavailable','\n'.join(BuiltinRegistry().prepare(self.root,records,row)[1]))
        self.assertEqual(BuiltinRegistry().prepare(self.root,[],dict(kind='event',payload={'future':'kind'}))[0],'Generic evidence')
    def test_slow_and_stale_jobs_do_not_replace_new_selection_and_resize_reuses_analysis(self):
        job=VisualizerJob(self.root);gate=threading.Event();calls=[]
        def slow(directory,records,row,mode):
            calls.append(row['payload']['name'])
            if row['payload']['name']=='old':gate.wait(2)
            return row['payload']['name'],['prepared']
        with patch.object(BuiltinRegistry,'prepare',side_effect=slow):
            old=dict(kind='event',payload={'name':'old'});new=dict(kind='event',payload={'name':'new'})
            self.assertIn('Preparing',job.tick('old',[],old)[0])
            self.assertIn('Preparing',job.tick('new',[],new)[0]);gate.set()
            deadline=time.monotonic()+3
            while job.current_key!='new' and time.monotonic()<deadline:
                title,lines=job.tick('new',[],new);time.sleep(.01)
            self.assertEqual(title,'new')
            for _ in range(5):self.assertEqual(job.tick('new',[],new)[0],'new')
            self.assertEqual(calls,['old','new'])

class HistoricalPtyTests(unittest.TestCase):
    def test_saved_source_diff_renders_while_agent_remains_interactive(self):
        import os,sys
        from labradour.pty_process import PtyProcess
        from labradour.terminal import Terminal
        root_repo=Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();workspace=root/'workspace';workspace.mkdir()
            (workspace/'x.py').write_text('alpha\n')
            script=root/'fixture.py';script.write_text("from pathlib import Path\nimport sys\nprint('READY',flush=True)\nfor line in sys.stdin:\n Path('x.py').write_text('beta\\n')\n print('CHANGED',flush=True)\n")
            child=PtyProcess([sys.executable,'-m','labradour','run','--workspace',str(workspace),
                '--record',str(root/'recording'),'--',sys.executable,str(script)],root_repo,40,140,
                dict(os.environ,XDG_CONFIG_HOME=str(root/'xdg')))
            terminal=Terminal(40,140)
            def wait_frame(predicate):
                deadline=time.monotonic()+6
                while time.monotonic()<deadline:
                    replies=terminal.feed(child.read())
                    if replies:child.send(replies)
                    if predicate(terminal.display):return
                    time.sleep(.02)
                self.fail('missing frame: '+'\n'.join(terminal.display))
            try:
                wait_frame(lambda rows:'READY' in '\n'.join(rows))
                child.send(b'write\r')
                wait_frame(lambda rows:'file.modify' in '\n'.join(rows))
                child.send(b'\x1dl/path:x.py tool:file.modify\rfd')
                wait_frame(lambda rows:'Source diff' in '\n'.join(rows) and '+beta' in '\n'.join(rows))
                child.send(b'e')
                wait_frame(lambda rows:'Generic evidence' in '\n'.join(rows))
                child.send(b'\x1b[C\x1b[Ds')
                wait_frame(lambda rows:'Source diff' in '\n'.join(rows) and '+beta' in '\n'.join(rows))
                child.send(b'\x1damore\r')
                wait_frame(lambda rows:'CHANGED' in '\n'.join(rows))
                child.send(b'\x11')
                deadline=time.monotonic()+4
                while child.poll() is None and time.monotonic()<deadline:child.read();time.sleep(.02)
                self.assertEqual(child.poll(),0)
            finally:child.close()
