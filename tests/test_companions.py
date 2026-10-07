import hashlib
import json
import os
import subprocess
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from labradour.companions import OpenFailure, launch, open_graphical, read_artifact, render
from labradour.exports import ArtifactCache, ExportError, ExportJob


class CompanionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.cache = ArtifactCache(self.root / 'cache')
        self.core = {'provenance':{'session_id':'saved-session', 'selection_id':'effect:original',
            'before_commit':'before', 'checkpoint':'after', 'file_attribution':'external-or-unknown'},
            'evidence':{'selection':{'kind':'effect','payload':{'path':'example.py'}},
                'before':{'state':'available','content_base64':'YWxwaGEK','mode':'100644'},
                'after':{'state':'available','content_base64':'YmV0YQo=','mode':'100644'},
                'gitdiffviz':{'scene':{'scene':{'nodes':[{'path':'','kind':'directory'},
                    {'path':'example.py','kind':'file'}], 'edges':[]}}}}}
        self.source = self.cache.publish(self.core)

    def test_completed_companion_is_private_static_escaped_and_explicit(self):
        with patch('labradour.companions.launch') as opener:
            self.assertFalse(list(self.cache.path.glob('*.html')))
            result = open_graphical(self.cache, self.source)
            opener.assert_called_once()
        path = Path(result['companion']['path'])
        content = path.read_text()
        self.assertIn('Captured file structure', content)
        self.assertIn('alpha', content)
        self.assertIn('beta', content)
        self.assertIn('saved-session', content)
        self.assertIn("script-src 'none'", content)
        self.assertIn("default-src 'none'", content)
        self.assertNotIn('<script', content)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(result['companion']['sha256'], hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual(result['id'], self.source['id'])
        with patch('labradour.companions.launch'):
            repeated = open_graphical(self.cache, self.source)
        self.assertTrue(repeated['companion']['cached'])

    def test_recorded_markup_and_plugin_urls_cannot_be_executed_or_launched(self):
        self.core['evidence']['selection']['payload']['path'] = '<script>alert(1)</script>'
        self.core['evidence']['gitdiffviz']['scene']['scene']['nodes'][1]['path'] = '"><img src=https://attacker.invalid onerror=alert(1)>'
        source = self.cache.publish(self.core)
        source['path'] = 'https://attacker.invalid/arbitrary-plugin-url'
        with patch('labradour.companions.launch') as opener:
            result = open_graphical(self.cache, source)
        content = Path(result['companion']['path']).read_text()
        self.assertIn('&lt;script&gt;', content)
        self.assertNotIn('<img', content)
        path = opener.call_args[0][0]
        self.assertEqual(path.parent, self.cache.path)
        self.assertEqual(path.suffix, '.html')

    def test_modified_missing_unsafe_or_non_json_source_is_never_opened(self):
        with patch('labradour.companions.launch') as opener:
            bad = dict(self.source, id='https://example.invalid')
            with self.assertRaises(ExportError):
                open_graphical(self.cache, bad)
            path = Path(self.source['path'])
            path.write_text('modified')
            with self.assertRaisesRegex(ExportError, 'hash changed'):
                open_graphical(self.cache, self.source)
            path.unlink()
            path.symlink_to(self.root / 'other')
            with self.assertRaises(ExportError):
                open_graphical(self.cache, self.source)
            path.unlink()
            with self.assertRaises(OSError):
                open_graphical(self.cache, self.source)
            opener.assert_not_called()

    def test_opener_failure_preserves_ready_artifact_and_retry_source(self):
        with patch('labradour.companions.launch', side_effect=ExportError('desktop opener failed')):
            with self.assertRaises(OpenFailure) as failure:
                open_graphical(self.cache, self.source)
        self.assertEqual(failure.exception.result['open_status'], 'failed')
        self.assertTrue(Path(failure.exception.result['companion']['path']).is_file())
        self.assertTrue(Path(self.source['path']).is_file())

    def test_platform_dispatch_uses_only_local_file_and_bounds_helper(self):
        path = self.cache.path / ('a' * 64 + '.html')
        with patch('labradour.companions.sys.platform', 'darwin'), patch('labradour.companions.subprocess.run') as command:
            command.return_value.returncode = 0
            launch(path)
            self.assertEqual(command.call_args[0][0], ['/usr/bin/open', str(path)])
            self.assertEqual(command.call_args[1]['timeout'], 5)
        with patch('labradour.companions.sys.platform', 'linux'), patch('labradour.companions.shutil.which', return_value='/usr/bin/xdg-open'), patch('labradour.companions.subprocess.run') as command:
            command.return_value.returncode = 0
            launch(path)
            self.assertEqual(command.call_args[0][0], ['/usr/bin/xdg-open', path.as_uri()])
        with patch('labradour.companions.sys.platform', 'linux'), patch('labradour.companions.shutil.which', return_value=None):
            with self.assertRaisesRegex(ExportError, 'no xdg-open'):
                launch(path)
        for failure, message in [(OSError('unavailable'), 'opener unavailable'),
                                 (subprocess.TimeoutExpired('open', 5), 'timed out')]:
            with patch('labradour.companions.sys.platform', 'darwin'), patch('labradour.companions.subprocess.run', side_effect=failure):
                with self.assertRaisesRegex(ExportError, message):
                    launch(path)

    def test_html_shares_quota_cleanup_and_cancellation_without_partial_files(self):
        cache = ArtifactCache(self.root / 'small', artifact_limit=10000, budget=10000, count=2)
        for index in range(4):
            cache.publish_html(('<html>' + str(index) + '</html>').encode(), {})
        self.assertEqual(len(cache.inspect()['artifacts']), 2)
        def cancelled():
            if list(cache.path.glob('.pending-*')):
                raise ExportError('cancelled')
        with self.assertRaises(ExportError):
            cache.publish_html(b'new HTML', {}, cancelled)
        self.assertFalse(list(cache.path.glob('.pending-*')))
        with cache.locked():
            cache.clean(clear=True)
        self.assertEqual(cache.inspect()['bytes'], 0)

    def test_worker_open_requires_ready_result_is_nonblocking_and_frozen(self):
        job = ExportJob(self.cache)
        with patch('labradour.companions.launch') as opener:
            self.assertFalse(job.open_graphical())
            opener.assert_not_called()
        job.result = self.source
        with patch('labradour.companions.launch') as opener:
            started = time.monotonic()
            self.assertTrue(job.open_graphical())
            self.assertLess(time.monotonic() - started, .1)
            self.assertFalse(job.open_graphical())
            deadline = time.monotonic() + 3
            while job.pending and time.monotonic() < deadline:
                job.poll()
                time.sleep(.01)
        self.assertFalse(job.pending)
        self.assertEqual(job.result['selection_id'], 'effect:original')
        self.assertEqual(job.result['open_status'], 'requested')

    def test_cli_opens_only_explicit_completed_id_and_hash(self):
        from labradour.__main__ import main
        import io
        import contextlib
        args = ['labradour','open-export',self.source['id'],'--sha256',self.source['sha256'],
                '--export-cache',str(self.cache.path)]
        output = io.StringIO()
        with patch('sys.argv', args), patch('labradour.companions.launch') as opener, contextlib.redirect_stdout(output):
            main()
        self.assertEqual(json.loads(output.getvalue())['open_status'], 'requested')
        opener.assert_called_once()

    def test_metadata_binary_empty_and_newline_content_remain_distinct(self):
        for side, expected in [({'state':'absent'},'absent'), ({'state':'metadata_only','reason':'policy'},'policy'),
            ({'state':'unavailable','reason':'pruned'},'pruned'),
            ({'state':'available','content_base64':'AA=='},'Binary/non-UTF-8'),
            ({'state':'available','content_base64':''},'[empty captured file]')]:
            self.core['evidence']['after'] = side
            content = render({'provenance':self.core['provenance'],'evidence':self.core['evidence']}, 'source').decode()
            self.assertIn(expected, content)
        self.core['evidence']['after'] = {'state':'available','content_base64':'Cg=='}
        self.assertNotIn('[empty captured file]', render(self.core, 'source').decode())
