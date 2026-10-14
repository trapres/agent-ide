"""Exercise desktop IPC against real PTYs and durable recordings, without a GUI."""
import base64
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]


class Client:
    def __init__(self):
        self.process = subprocess.Popen([sys.executable, '-u', '-m', 'labradour.desktop'],
            cwd=str(ROOT), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.buffer = bytearray()
        self.messages = []
        self.terminal = b''
        self.activity = {}
        self.serial = 0
        self.wait(lambda: any(m.get('event') == 'ready' for m in self.messages))

    def pump(self):
        if select.select([self.process.stdout], [], [], .05)[0]:
            chunk = os.read(self.process.stdout.fileno(), 65536)
            if not chunk:
                raise AssertionError('Backend exited: ' + self.process.stderr.read().decode())
            self.buffer.extend(chunk)
            while b'\n' in self.buffer:
                line, _, rest = self.buffer.partition(b'\n')
                self.buffer = bytearray(rest)
                message = json.loads(line)
                self.messages.append(message)
                if message.get('event') == 'terminal':
                    self.terminal += base64.b64decode(message['payload']['data'])
                if message.get('event') == 'activity':
                    self.activity = message['payload']

    def wait(self, predicate, timeout=12):
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() > deadline:
                raise AssertionError('Desktop response timed out')
            self.pump()

    def call(self, method, **args):
        self.serial += 1
        request_id = self.serial
        self.process.stdin.write((json.dumps({'id': request_id, 'method': method, 'args': args}) + '\n').encode())
        self.process.stdin.flush()
        self.wait(lambda: any(m.get('id') == request_id for m in self.messages))
        result = next(m for m in self.messages if m.get('id') == request_id)
        if 'error' in result:
            raise ValueError(result['error'])
        return result.get('result')

    def send(self, text):
        self.call('input', data=base64.b64encode(text.encode()).decode())

    def close(self):
        if self.process.poll() is None:
            self.call('shutdown')
            self.process.wait(timeout=12)
        if self.process.returncode != 0:
            raise AssertionError(self.process.stderr.read().decode())
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            stream.close()


class DesktopTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir()
        (self.workspace / 'example.py').write_text('alpha\n')
        self.client = Client()

    def tearDown(self):
        self.client.close()
        self.temporary.cleanup()

    def launch(self):
        return self.client.call('launch', workspace=str(self.workspace),
            recording=str(self.root / 'recording'), cache=str(self.root / 'cache'),
            command=['/bin/sh'], rows=24, columns=80)

    def test_provider_model_is_passed_as_an_argument(self):
        from labradour.desktop import provider_command
        self.assertEqual(provider_command('codex', 'chosen-model'), ['codex', '--no-daemon', '--model', 'chosen-model'])
        self.assertEqual(provider_command('codex', 'chosen-model', hooks=True), ['codex', '--model', 'chosen-model'])
        self.assertEqual(provider_command('claude', 'opus'), ['claude', '--model', 'opus'])
        self.assertEqual(provider_command('claude'), ['claude'])
        self.assertEqual(provider_command('shell', 'ignored'), ['/bin/sh'])

    def test_terminal_resize_unicode_and_process_shutdown(self):
        self.launch()
        self.client.call('resize', rows=31, columns=93)
        self.client.send("stty size; printf 'café 界\\n'\n")
        self.client.wait(lambda: b'31 93' in self.client.terminal and 'café 界'.encode() in self.client.terminal)
        self.client.send('echo OWNED_PID=$$\n')
        self.client.wait(lambda: b'OWNED_PID=' in self.client.terminal and
                         any(part.split(b'\r')[0].isdigit() for part in self.client.terminal.split(b'OWNED_PID=')[1:]))
        import re
        pid = int(re.search(rb'OWNED_PID=(\d+)', self.client.terminal).group(1))
        self.client.call('stop')
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        self.assertEqual(self.client.activity['status'], 'saved')
        self.assertEqual(self.client.activity['sessions'][-1]['status'], 'completed')

    def test_recorded_edit_survives_revert_and_saved_review(self):
        info = self.launch()
        self.client.send("printf 'beta\\n' > example.py\n")
        def edits():
            return [r for r in self.client.activity.get('rows', []) if r['kind'] == 'effect'
                    and r['tool'] == 'file.modify']
        self.client.wait(lambda: len(edits()) >= 1)
        edit = edits()[0]
        self.client.send("printf 'alpha\\n' > example.py\n")
        self.client.wait(lambda: len(edits()) >= 2)
        self.client.call('stop')
        self.client.call('review', recording=info['recording'], session=info['session'], cache=str(self.root / 'cache'))
        view = self.client.call('job', row=edit['id'], operation='view')
        self.assertIn('-alpha', view['lines'])
        self.assertIn('+beta', view['lines'])
        artifact = self.client.call('job', row=edit['id'], operation='export')
        document = json.loads(Path(artifact['path']).read_text())
        self.assertEqual(base64.b64decode(document['evidence']['after']['content_base64']), b'beta\n')
        self.assertEqual((self.workspace / 'example.py').read_text(), 'alpha\n')
        whole = self.client.call('job', operation='session-export')
        self.assertTrue(Path(whole['path']).is_file())

    def test_failed_request_preserves_native_input_and_eof_cleans_up(self):
        self.launch()
        with self.assertRaisesRegex(ValueError, 'Stop the current'):
            self.launch()
        with self.assertRaisesRegex(ValueError, 'no longer available'):
            self.client.call('job', row='missing', operation='gitdiffviz')
        self.client.send("printf 'STILL_RUNNING\\n'\n")
        self.client.wait(lambda: b'STILL_RUNNING\r\n' in self.client.terminal)
        self.client.process.stdin.close()
        self.client.process.wait(timeout=12)
        self.assertEqual(self.client.process.returncode, 0)


if __name__ == '__main__':
    unittest.main()
