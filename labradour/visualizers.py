"""Historical built-ins and bounded read-only evidence; no workspace fallback."""
import difflib
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import threading
import time
from .leases import evidence_lease

LIMIT = 256 * 1024
MAX_LINES = 4000


class EvidenceReader:
    def __init__(self, directory, records):
        self.repo = Path(directory).resolve() / 'history.git'
        self.snapshots = [r for r in records if r['kind'] == 'snapshot.completed']
        self.env = {k:v for k,v in os.environ.items() if not k.startswith('GIT_')}
        self.env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull, GIT_NO_REPLACE_OBJECTS='1')

    def git(self, *args):
        remaining = getattr(self, 'deadline', time.monotonic() + 2) - time.monotonic()
        if remaining <= 0:
            raise ValueError('evidence read deadline exceeded')
        result = subprocess.run(['git', '--git-dir='+str(self.repo), *args], env=self.env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=min(2, remaining))
        if result.returncode:
            raise ValueError('saved Git object unavailable (possibly pruned)')
        return result.stdout

    def describe(self, snapshot, path):
        if not snapshot:
            return {'state':'unavailable','reason':'missing captured checkpoint'}
        if not isinstance(path,str) or not path or path.startswith('/') or '\0' in path or any(
                part in ('','..','.','.git') for part in path.split('/')):
            raise ValueError('invalid granted historical path')
        p = snapshot['payload']; commit=p.get('commit')
        if not isinstance(commit,str) or not re.fullmatch('[0-9a-f]{40}',commit):
            return {'state':'unavailable','reason':'missing checkpoint commit'}
        if p.get('payload_truncated'):
            return {'state':'unavailable','reason':'checkpoint metadata truncated'}
        for item in p.get('metadata',[]):
            if item.get('path')==path and item.get('type')!='directory':
                return dict(state='metadata_only', reason=item.get('reason',item.get('type')), metadata=item)
        stale = any(path==i.get('path') or path.startswith(str(i.get('path'))+'/') or i.get('path')=='.'
                    for i in p.get('issues',[]))
        try:
            self.git('cat-file','-e',commit+'^{commit}')
            expression=commit+':'+path
            # Exact byte-name match prevents wildcard pathspec matches granting another file.
            listing=self.git('ls-tree','-z',commit,'--',path)
            mode=None
            for entry in listing.split(b'\0'):
                if b'\t' in entry:
                    meta,name=entry.split(b'\t',1)
                    if name==os.fsencode(path): mode=meta.split()[0].decode()
            if mode is None:
                return {'state':'unavailable' if stale else 'absent', 'reason':'read error' if stale else 'path absent in captured tree'}
            size=int(self.git('cat-file','-s',expression))
            if size>LIMIT:
                return {'state':'metadata_only','reason':'visualizer read limit', 'size':size}
            data=self.git('cat-file','blob',expression)
            if len(data)>LIMIT:
                return {'state':'unavailable','reason':'read exceeded limit'}
            return {'state':'available','bytes':data,'size':len(data),'mode':mode,
                    'checkpoint':commit,'stale':stale,'reason':'retained older bytes after read error' if stale else None}
        except (ValueError,OSError,subprocess.TimeoutExpired):
            return {'state':'unavailable','reason':'saved object missing/pruned or read failed'}

    def effect(self, effect):
        # Only a recorded effect grants this path and its recorded checkpoint pair.
        snapshot=next((s for s in self.snapshots if s['event_id']==effect['snapshot_event_id']),None)
        if snapshot is None or not any(c.get('path')==effect['path'] and c.get('operation')==effect['operation']
                                     for c in snapshot['payload'].get('changes',[])):
            raise ValueError('effect is not granted by recorded snapshot')
        if snapshot['payload'].get('commit')!=effect.get('checkpoint') or snapshot['payload'].get('before_commit')!=effect.get('before_commit'):
            raise ValueError('effect checkpoint pair does not match captured evidence')
        earlier=[s for s in self.snapshots if s['sequence']<snapshot['sequence'] and
                 s['payload'].get('commit')==effect.get('before_commit')]
        try:
            with evidence_lease(self.repo.parent):
                self.deadline = time.monotonic() + 5
                return self.describe(earlier[-1] if earlier else None,effect['path']),self.describe(snapshot,effect['path'])
        except (ValueError, OSError) as exc:
            unavailable = {'state':'unavailable', 'reason':str(exc)}
            return dict(unavailable), dict(unavailable)


def text_content(item):
    if item['state']!='available': return None
    data=item['bytes']
    if b'\0' in data: return None
    try: return data.decode('utf-8')
    except UnicodeError: return None


def json_lines(value):
    # Captured arguments/results may be larger; derived display stays bounded.
    pieces=[];size=0;truncated=False
    for piece in json.JSONEncoder(ensure_ascii=True,indent=2).iterencode(value):
        available=LIMIT-size
        pieces.append(piece[:available]);size+=min(len(piece),available)
        if len(piece)>available:
            truncated=True;break
    return ''.join(pieces).splitlines()+(['[display truncated]'] if truncated else [])


class BuiltinRegistry:
    ids=('file-create','source-diff','file-delete','command','generic')

    def prepare(self,directory,records,row,mode='auto'):
        payload=row['payload']
        lines=['File attribution: external-or-unknown; candidate intervals are not ownership.']
        title='Generic evidence'
        if mode=='evidence':
            ids=set(payload.get('observations',[])) if row['kind']=='action' else set()
            evidence={'selection':payload,'observations':[r for r in records if r['event_id'] in ids]}
            evidence_lines=lines+json_lines(evidence)
            return title,evidence_lines[:MAX_LINES]+(['[evidence display truncated]'] if len(evidence_lines)>MAX_LINES else [])
        if row['kind']=='effect':
            title={'file.create':'File created','file.modify':'Source diff','file.delete':'File deleted'}.get(payload['operation'],'File metadata/evidence')
            lines += ['Path: '+ascii(payload['path']), 'Operation: '+payload['operation'],
                'Interval: %s -> %s' % (payload.get('before_commit'),payload.get('checkpoint')),
                'Correlation: '+payload['quality']+' | capture: '+payload['capture_quality']]
            before,after=EvidenceReader(directory,records).effect(payload)
            for name,item in (('Before',before),('After',after)):
                lines.append('%s: %s | %s' % (name,item['state'],item.get('reason') or
                    '%s bytes; mode %s' % (item.get('size'),item.get('mode'))))
                if item.get('stale'): lines.append('WARNING: retained bytes are stale; original checkpoint provenance is not established.')
            a,b=text_content(before),text_content(after)
            operation=payload['operation']
            if operation=='file.create' and b is not None:
                if before['state']!='absent':
                    title='First captured file'
                    lines.append('Creation boundary unavailable; first captured content only.')
                lines += ['Captured new content:']+(b.splitlines() or ['[empty file]'])
            elif operation=='file.delete' and after['state']=='absent' and a is not None:
                lines += ['Last captured content:']+(a.splitlines() or ['[empty file]'])
            elif operation=='file.modify' and a is not None and b is not None:
                a_lines,b_lines=a[:65536].splitlines(),b[:65536].splitlines()
                limited=len(a)>65536 or len(b)>65536 or len(a_lines)>2000 or len(b_lines)>2000
                lines += list(difflib.unified_diff(a_lines[:2000],b_lines[:2000],fromfile='captured before',tofile='captured after',lineterm='')) or ['No text changes in displayed comparison; inspect modes/metadata.']
                if limited: lines.append('[diff display truncated: 64 KiB / 2000 lines per side]')
                if a.endswith('\n')!=b.endswith('\n'):
                    lines.append('Final newline changed: before=%s after=%s' % (a.endswith('\n'),b.endswith('\n')))
            else:
                lines += ['Incomplete comparison, omitted content, binary/non-UTF-8 data or unsupported operation.']
            if before.get('mode')=='120000' or after.get('mode')=='120000':
                lines.append('Symlink target bytes only; destination is never followed.')
        elif row['kind']=='action':
            title='Command / tool' if any(word in (payload.get('tool') or '').lower() for word in ('bash','shell','command')) else 'Tool observation'
            lines += ['Tool: '+str(payload.get('tool')), 'Lifecycle: '+payload['state'],
                'Result outcomes: '+str(payload['result_outcomes']),
                'Evidence: '+payload['correlation_quality'], 'Identity: '+payload['identity_quality']]
            lines += payload['notes']
            events={r['event_id']:r for r in records}
            outputs=[]
            for eid in payload['observations'][:64]:
                raw=events[eid]['payload'].get('payload',{}).get('raw',{})
                if not isinstance(raw,dict): continue
                for key in ('tool_input','tool_response','error'):
                    if key in raw:
                        lines += [key+':']+json_lines(raw[key])
                        if key=='tool_response': outputs.append(raw[key])
            if not outputs: lines.append('Output unavailable: no recorded tool response (not empty output).')
            if len(payload['observations'])>64: lines.append('[tool observations display truncated at 64]')
            lines += ['Candidate effects: '+str(len(row['children'])), 'Use Activity Right/Enter to select effects; Visualization ]/[ cycles effects; s returns summary.']
        else:
            lines += json_lines(payload)
        bounded=[];remaining=LIMIT
        for line in lines[:MAX_LINES]:
            if len(line)+1>remaining:
                bounded.extend([line[:remaining],'[view text truncated]']);break
            bounded.append(line);remaining-=len(line)+1
        if len(lines)>MAX_LINES: bounded.append('[view truncated at %d lines]'%MAX_LINES)
        return title,bounded


class VisualizerJob:
    def __init__(self,directory):
        self.directory=directory
        self.pending=False
        self.results=queue.Queue(maxsize=1)
        self.current_key=None
        self.title='Visualization'
        self.lines=['Select an action/effect.']

    def tick(self,key,records,row,mode='auto'):
        try:
            done,title,lines=self.results.get_nowait()
            self.pending=False
            if done==key:
                self.current_key=done; self.title=title; self.lines=lines
        except queue.Empty: pass
        if key!=self.current_key and not self.pending:
            self.pending=True
            copied_records=tuple(records)
            def prepare():
                try: title,lines=BuiltinRegistry().prepare(self.directory,copied_records,row,mode)
                except Exception as exc: title,lines='Evidence unavailable',['Cannot prepare historical view: '+str(exc)]
                self.results.put((key,title,lines))
            threading.Thread(target=prepare,name='labradour-visualizer',daemon=True).start()
        if self.current_key!=key:
            return 'Preparing historical view',['Loading recorded evidence; agent and recorder continue.']
        return self.title,self.lines
