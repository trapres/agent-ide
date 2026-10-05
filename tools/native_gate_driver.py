"""Private interactive PTY driver for native gate experiments (JSON commands on stdin)."""
import argparse
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from labradour.pty_process import PtyProcess
from labradour.recorder import read_history
from labradour.terminal import Terminal


def active_calls(store, marker):
    if not (store / 'journal.sqlite').exists():
        return []
    sessions = read_history(store)
    if not sessions:
        return []
    rows = read_history(store, sessions[-1]['id'])
    terminal = set()
    starts = {}
    captured = set()
    for r in rows:
        p = r['payload']
        if r['kind'] == 'adapter.boundary' and p.get('phase') == 'before' and p.get('completed'):
            captured.add((p.get('launch_id'), p.get('adapter_event_id')))
        if r['kind'] != 'adapter.event':
            continue
        identity = tuple(p.get(k) for k in ('launch_id','provider','provider_session_id','actor_id','turn_id','call_id'))
        if not p.get('call_id'):
            continue
        if p.get('kind') in ('tool.completed','tool.failed','tool.denied','tool.interrupted'):
            terminal.add(identity)
        if p.get('kind') == 'tool.requested' and p.get('payload', {}).get('tool') == 'Bash':
            # Match our own experiment marker for synchronization only; never
            # use command text as an attribution or outcome parser.
            detail = p.get('payload',{}).get('raw',{}).get('tool_input',{})
            if marker in json.dumps(detail) and not detail.get('run_in_background', False):
                starts[identity] = p
    return [p for identity,p in starts.items() if identity not in terminal
            and (p.get('launch_id'), p['event_id']) in captured]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--trace-terminal', action='store_true', help='Save private native terminal bytes for independent replay')
    parser.add_argument('provider', choices=('claude','codex'))
    parser.add_argument('root',type=Path)
    parser.add_argument('arguments',nargs=argparse.REMAINDER)
    args=parser.parse_args()
    root=args.root.resolve(); workspace=root/'workspace'; store=root/'recording'
    extra=args.arguments[1:] if args.arguments[:1]==['--'] else args.arguments
    launcher=[sys.executable,'-m','labradour']
    if args.trace_terminal:
        # Instrument the native pane inside the wrapper, not the outer screen.
        trace = str(root/'native-terminal.bin'); state = str(root/'native-terminal.json')
        Path(trace).write_bytes(b'')
        Path(trace).chmod(0o600)
        code = ('import json,runpy; from pathlib import Path; from labradour.terminal import Terminal; '
                'original=Terminal.feed\n'
                'def feed(self,data):\n'
                ' with open(%r,"ab") as f: f.write(data)\n'
                ' reply=original(self,data)\n'
                ' Path(%r).write_text(json.dumps({"rows":self.rows,"columns":self.columns,"display":self.display}))\n'
                ' return reply\n'
                'Terminal.feed=feed\nrunpy.run_module("labradour",run_name="__main__")') % (trace,state)
        launcher=[sys.executable,'-c',code]
    child=PtyProcess([*launcher,'run','--workspace',str(workspace),'--record',str(store),
                      '--hooks',args.provider,'--',args.provider,*extra],Path(__file__).resolve().parents[1],40,140)
    terminal=Terminal(40,140)
    def log(data):
        with (root/'driver-evidence.jsonl').open('a') as out:
            out.write(json.dumps(dict(time_ns=time.time_ns(),**data))+'\n')
    def drain():
        output=child.read()
        if output:
            replies=terminal.feed(output)
            if replies:child.send(replies)
        child.flush()
    def pump(seconds):
        deadline=time.monotonic()+seconds
        while time.monotonic()<deadline and child.poll() is None:
            drain(); time.sleep(.02)
        drain()
    def show():
        print(json.dumps({'root':str(root),'pid':child.pid,'exit':child.poll(),
                          'screen':terminal.display},ensure_ascii=False),flush=True)
    try:
        pump(4); show()
        for line in sys.stdin:
            command=json.loads(line)
            if command.get('stop'):break
            if 'send' in command:child.send(command['send'].encode())
            if 'resize' in command:
                rows,columns=command['resize']; child.resize(rows,columns); terminal.resize(rows,columns)
            if 'external_when_active' in command:
                spec=command['external_when_active']; deadline=time.monotonic()+min(spec.get('timeout',60),60)
                active=[]
                while time.monotonic()<deadline and child.poll() is None:
                    drain(); active=active_calls(store,spec['marker'])
                    if len(active)>=spec['count']:break
                    time.sleep(.05)
                if len(active)>=spec['count']:
                    path=workspace/spec['path']
                    if path.is_absolute() and (workspace not in path.resolve().parents):
                        raise ValueError('external fixture path must stay inside workspace')
                    path.write_text(spec['content'])
                    log({'experiment':'external-write','path':spec['path'],'active_call_count':len(active),
                         'active_provider_event_ids':[p['event_id'] for p in active],
                         'actors':[p.get('actor_id') for p in active]})
                else:log({'experiment':'external-write','status':'not-run','reason':'no matching overlapping calls'})
            pump(min(command.get('wait',1),60))
            if 'save_screen' in command:
                name=command['save_screen']
                if Path(name).name!=name:raise ValueError('screen filename must be local')
                (root/name).write_text(json.dumps(terminal.display,ensure_ascii=False))
            show()
    finally:
        stopped=child.close(); log({'experiment':'driver-close','reaped':stopped,'launcher_status':child.status})
        print(json.dumps({'reaped':stopped,'status':child.status}),flush=True)


if __name__=='__main__':main()
