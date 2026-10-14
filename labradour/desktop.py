"""JSON-lines desktop bridge: PTY/recording ownership stays outside the webview."""
import base64
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import select
import signal
import sys
import tempfile
import threading
import time

from .activity import build_rows
from .adapters.correlation import project
from .exports import ArtifactCache, evidence_export, session_export
from .gitdiffviz import gitdiffviz_export
from .leases import evidence_lease
from .pty_process import PtyProcess
from .recorder import Recorder, read_history
from .visualizers import BuiltinRegistry


def provider_command(provider, model='', hooks=False):
    if provider not in ('codex', 'claude', 'shell'):
        raise ValueError('Choose Codex, Claude, or Shell')
    command = ['/bin/sh' if provider == 'shell' else provider]
    if provider == 'codex' and not hooks:
        command.append('--no-daemon')
    if model and provider != 'shell':
        command.extend(['--model', model])
    return command


class Bridge:
    def __init__(self):
        self.output_lock = threading.Lock()
        self.child = self.recorder = self.collector = None
        self.directory = self.session = self.workspace = None
        self.temporary = None
        self.records = []
        self.status = 'idle'
        self.active = True
        self.executor = None  # Create workers only after the first PTY fork.
        self.future = None
        self.refresh_future = None
        self.cancel = threading.Event()
        self.last_refresh = 0
        self.revision = None
        self.config = None
        self.cache = None

    def emit(self, event, payload):
        self.write({'event': event, 'payload': payload})

    def write(self, value):
        with self.output_lock:
            print(json.dumps(value, ensure_ascii=True), flush=True)

    def close_session(self):
        self.cancel.set()
        # Finish background readers before releasing/remapping their source session.
        if self.future:
            try:
                self.future.result(timeout=35)
            except Exception:
                pass
        if self.executor:
            self.executor.shutdown(wait=True)
            self.executor = None
        self.future = None
        self.refresh_future = None
        if self.child:
            self.child.close()
            self.child = None
        if self.collector:
            self.collector.close()
        if self.recorder:
            self.recorder.close()
            self.recorder = None
        self.collector = None
        if self.temporary:
            self.temporary.cleanup()
            self.temporary = None

    def launch(self, args):
        if self.child is not None or self.recorder is not None:
            raise ValueError('Stop the current agent before starting another session')
        self.close_session()
        self.temporary = tempfile.TemporaryDirectory(prefix='labradour-desktop-')
        temporary = Path(self.temporary.name)
        demo = bool(args.get('demo'))
        self.workspace = temporary / 'workspace' if demo else Path(args['workspace']).expanduser().resolve()
        if demo:
            self.workspace.mkdir()
        if not self.workspace.is_dir():
            raise ValueError('Workspace must be an existing directory')
        self.directory = Path(args.get('recording') or tempfile.mkdtemp(prefix='labradour-recording-')).expanduser().resolve()
        self.cache = Path(args.get('cache') or tempfile.mkdtemp(prefix='labradour-exports-')).expanduser().resolve()
        self.config = Path(args['config']).expanduser().resolve() if args.get('config') else None
        events = temporary / 'events'
        events.mkdir()
        command = ([sys.executable, '-u', str(Path(__file__).with_name('demo.py'))] if demo else
                   provider_command(args['provider'], args.get('model', '').strip(), bool(args.get('hooks'))) if args.get('provider') else args.get('command'))
        if not isinstance(command, list) or not command or any(not isinstance(x, str) for x in command):
            raise ValueError('Command must be a nonempty array of arguments')
        env = dict(os.environ, LABRADOUR_EVENTS_DIR=str(events))
        for key in tuple(env):
            if key.startswith('LABRADOUR_COLLECTOR_') or key in ('LABRADOUR_LAUNCH_ID', 'LABRADOUR_WORKSPACE_ID'):
                env.pop(key)
        provider = args.get('hooks')
        if provider:
            from .adapters.collector import Collector
            from .adapters.launch import compose
            self.collector = Collector(self.workspace, events)
            env.update(self.collector.environment())
            command = compose(provider, command, temporary, self.workspace, events, authenticated=True)
        # Fork before watchdog, collectors, and view workers have started threads.
        self.child = PtyProcess(command, self.workspace, int(args.get('rows', 24)),
                                int(args.get('columns', 80)), env, paused=True)
        try:
            self.recorder = Recorder(self.workspace, self.directory, excluded=[events, self.cache],
                                     events=events, adapter_collector=self.collector,
                                     adapter_provider=provider)
            self.recorder.start()
            if self.collector:
                self.collector.start()
            self.session = self.recorder.session
            self.status = 'live'
            self.records = []
            self.revision = None
            self.child.release()
            self.refresh(force=True)
        except Exception:
            self.close_session()
            raise
        return {'recording': str(self.directory), 'session': self.session, 'workspace': str(self.workspace)}

    def review(self, args):
        if self.child is not None or self.recorder is not None:
            raise ValueError('Stop the current agent before opening saved review')
        self.close_session()
        directory = Path(args['recording']).expanduser().resolve()
        with evidence_lease(directory):
            sessions = read_history(directory)
        if not sessions:
            raise ValueError('Recording has no sessions')
        session = args.get('session') or sessions[-1]['id']
        if session not in {s['id'] for s in sessions}:
            raise ValueError('Unknown recording session')
        self.directory, self.session, self.workspace = directory, session, None
        self.config = Path(args['config']).expanduser().resolve() if args.get('config') else None
        self.cache = Path(args.get('cache') or tempfile.mkdtemp(prefix='labradour-exports-')).expanduser().resolve()
        self.status = 'saved'
        self.revision = None
        self.refresh(force=True)
        return {'session': session, 'sessions': sessions, 'recording': str(directory)}

    def refresh(self, force=False):
        if self.directory is None:
            return
        if self.recorder:
            self.recorder.drain()
        def read():
            with evidence_lease(self.directory):
                return read_history(self.directory, self.session), read_history(self.directory)
        if force:
            records, sessions = read()
        elif self.refresh_future:
            if not self.refresh_future.done():
                return
            records, sessions = self.refresh_future.result()
            self.refresh_future = None
        else:
            if time.monotonic() - self.last_refresh < .4:
                return
            self.last_refresh = time.monotonic()
            if not self.executor:
                self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='desktop-view')
            self.refresh_future = self.executor.submit(read)
            return
        revision = (len(records), records[-1]['sequence'] if records else 0, self.status)
        if revision == self.revision and not force:
            return
        self.records, self.revision = records, revision
        projection = project(records, 'running' if self.status == 'live' else 'closed')
        rows = build_rows(records, projection)
        self.emit('activity', {'rows': rows, 'status': self.status, 'session': self.session,
                              'recording': str(self.directory),
                              'health': self.recorder.notice if self.recorder else 'Saved evidence',
                              'sessions': sessions})

    def job(self, request_id, args):
        if self.future and not self.future.done():
            raise ValueError('A view/export is pending; cancel it or wait')
        records, revision = self.records[:], self.revision
        directory, session, config, cache_path = self.directory, self.session, self.config, self.cache
        if directory is None:
            raise ValueError('Launch an agent or open a recording first')
        rows = build_rows(records, project(records, 'running' if self.status == 'live' else 'closed'))
        row = next((r for parent in rows for r in [parent, *parent['children']]
                    if r['id'] == args.get('row')), None)
        operation = args.get('operation', 'view')
        if operation != 'session-export' and row is None:
            raise ValueError('Selected row is no longer available')
        self.cancel = threading.Event()
        cancel = self.cancel
        deadline = time.monotonic() + 30

        def check():
            if cancel.is_set():
                raise ValueError('Cancelled')
            if time.monotonic() > deadline:
                raise ValueError('View/export deadline exceeded')

        def work():
            try:
                check()
                cache = ArtifactCache(cache_path)
                if operation == 'view':
                    title, lines = BuiltinRegistry().prepare(directory, records, row)
                    result = {'title': title, 'lines': lines, 'row': row['id']}
                elif operation == 'gitdiffviz':
                    result = gitdiffviz_export(directory, records, row, revision, cache, config, check)
                    document = json.loads(Path(result['path']).read_text())
                    result = dict(result, scene=document['evidence']['gitdiffviz']['scene'],
                                  provenance=document['provenance'], row=row['id'])
                elif operation == 'export':
                    result = evidence_export(directory, records, row, revision, cache, check)
                elif operation == 'session-export':
                    if self.status == 'live':
                        raise ValueError('Stop the agent before exporting the saved session')
                    result = session_export(directory, session, cache, check)
                else:
                    raise ValueError('Unknown view/export operation')
                check()
                self.write({'id': request_id, 'result': result})
            except Exception as exc:
                self.write({'id': request_id, 'error': str(exc)})
        if not self.executor:
            self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='desktop-view')
        self.future = self.executor.submit(work)

    def request(self, request):
        request_id = request.get('id')
        method, args = request.get('method'), request.get('args') or {}
        try:
            if method == 'launch':
                result = self.launch(args)
            elif method == 'review':
                result = self.review(args)
            elif method == 'input':
                if self.child:
                    self.child.send(base64.b64decode(args['data'], validate=True))
                result = None
            elif method == 'resize':
                if self.child and not self.child.eof:
                    self.child.resize(min(300, max(1, int(args['rows']))), min(500, max(1, int(args['columns']))))
                result = None
            elif method == 'job':
                self.job(request_id, args)
                return
            elif method == 'cancel':
                self.cancel.set()
                result = None
            elif method == 'stop':
                self.close_session()
                self.status = 'saved'
                self.refresh(force=True)
                result = None
            elif method == 'shutdown':
                self.active = False
                result = None
            else:
                raise ValueError('Unknown desktop request')
            self.write({'id': request_id, 'result': result})
        except Exception as exc:
            self.write({'id': request_id, 'error': str(exc)})

    def run(self):
        buffer = bytearray()
        for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            signal.signal(sig, lambda *_: setattr(self, 'active', False))
        self.emit('ready', {'protocol': 1})
        try:
            while self.active:
                if self.child:
                    output = self.child.read()
                    if output:
                        self.emit('terminal', {'data': base64.b64encode(output).decode()})
                    self.child.flush()
                    status = self.child.poll()
                    if status is not None and self.status == 'live':
                        self.status = 'exited'
                        self.emit('exit', {'code': status})
                self.refresh()
                if select.select([sys.stdin.fileno()], [], [], .02)[0]:
                    chunk = os.read(sys.stdin.fileno(), 65536)
                    if not chunk:
                        break
                    buffer.extend(chunk)
                    if len(buffer) > 2 * 1024 * 1024:
                        raise ValueError('Desktop input exceeds protocol limit')
                    while b'\n' in buffer:
                        line, _, rest = buffer.partition(b'\n')
                        buffer = bytearray(rest)
                        try:
                            self.request(json.loads(line))
                        except ValueError as exc:
                            self.emit('error', {'message': str(exc)})
        finally:
            self.close_session()


if __name__ == '__main__':
    Bridge().run()
