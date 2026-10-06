"""Read-only action/effect rows and stable selection, independent of curses."""
import queue
import threading
import time

from .adapters.correlation import project

TOOL_KINDS = {'tool.requested', 'tool.started', 'tool.completed', 'tool.failed',
              'tool.denied', 'tool.interrupted', 'approval.requested'}


def build_rows(records, projection=None, journal=False):
    projection = project(records) if projection is None else projection
    events = {r['event_id']: r for r in records}
    effects = {e['id']: e for e in projection['effects']}
    rows = []
    consumed = set()
    if not journal:
        for action in projection['actions']:
            consumed.update(action['observations'])
            observation = events[action['observations'][0]]
            raw = observation['payload'].get('payload', {}).get('raw', {})
            raw = raw if isinstance(raw, dict) else {}
            target = raw.get('tool_input', {})
            target = str(target)
            linked = [effects[eid] for eid in action['effect_ids']]
            scope = action['scope']
            row = {'id': action['id'], 'kind': 'action', 'sequence': observation['sequence'],
                   'actor': scope[4] or 'unknown', 'turn': scope[5] or 'unknown',
                   'tool': action['tool'] or 'unknown tool', 'state': action['state'],
                   'path': ' '.join(e['path'] for e in linked), 'target': target,
                   'payload': action, 'children': []}
            for effect in linked:
                row['children'].append(effect_row(effect, observation['sequence'], action['id']))
            rows.append(row)
        for effect in effects.values():
            if not effect['candidate_action_ids']:
                rows.append(effect_row(effect, events[effect['snapshot_event_id']]['sequence']))
    for record in records:
        if record['event_id'] in consumed:
            continue
        payload = record['payload']
        if not journal and record['kind'] in ('adapter.boundary', 'snapshot.intent'):
            continue
        if not journal and record['kind'] == 'snapshot.completed' and payload.get('changes'):
            continue
        detail = payload.get('payload', {}) if record['kind'] == 'adapter.event' else payload
        label = payload.get('kind', record['kind']) if record['kind'] == 'adapter.event' else record['kind']
        rows.append({'id': 'event:' + record['event_id'], 'kind': 'event', 'sequence': record['sequence'],
            'actor': payload.get('actor_id') or payload.get('actor') or 'external/unknown',
            'turn': payload.get('turn_id') or 'unknown', 'tool': detail.get('tool', payload.get('tool_name', 'event')),
            'state': label, 'path': str(payload.get('path', '')), 'target': str(label)[:256],
            'payload': record, 'children': []})
    return sorted(rows, key=lambda r: (r['sequence'], r['id']))


def effect_row(effect, sequence, parent=None):
    return {'id': effect['id'] + (':' + parent if parent else ''), 'kind': 'effect',
            'sequence': sequence, 'actor': 'external/unknown', 'turn': 'unknown',
            'tool': effect['operation'], 'state': effect['quality'], 'path': effect['path'],
            'target': effect['path'][:256], 'payload': effect, 'children': [], 'parent': parent}


class ActivityModel:
    def __init__(self):
        self.records = []
        self.projection = None
        self.rows = []
        self.selected_id = None
        self.expanded = set()
        self.follow = True
        self.unread = 0
        self.filter = ''
        self.journal = False
        self.revision = None
        self._visible_key = None
        self._visible = []

    def update(self, records, projection=None):
        projection = project(records) if projection is None else projection
        revision = (len(records), records[-1]['sequence'] if records else 0, projection['session_status'])
        if revision == self.revision:
            return
        self.revision = revision
        previous_ids = {r['id'] for r in self.rows}
        self.records, self.projection = records, projection
        self.rows = build_rows(records, self.projection, self.journal)
        if not self.follow:
            self.unread += len({r['id'] for r in self.rows} - previous_ids)
        else:
            self.resume()

    def visible(self):
        key = (self.revision, self.filter, self.journal, tuple(sorted(self.expanded)))
        if key == self._visible_key:
            return self._visible
        def matches(row):
            if not self.filter:
                return True
            haystack = ' '.join(str(row.get(k, '')) for k in ('actor', 'turn', 'tool', 'state', 'path', 'target')).casefold()
            for term in self.filter.casefold().split():
                key, separator, value = term.partition(':')
                if separator and key in ('actor', 'turn', 'tool', 'state', 'path'):
                    if value not in str(row.get(key, '')).casefold(): return False
                elif term not in haystack:
                    return False
            return True
        result = []
        for row in self.rows:
            children = [c for c in row['children'] if matches(c)]
            if matches(row) or children:
                result.append(row)
                if row['id'] in self.expanded or (self.filter and children):
                    result.extend(children)
        self._visible_key, self._visible = key, result
        return result

    def selected(self):
        for row in self.rows:
            if row['id'] == self.selected_id: return row
            for child in row['children']:
                if child['id'] == self.selected_id: return child
        return None

    def details(self):
        row = self.selected()
        if row is None: return None
        payload = row['payload']
        if row['kind'] == 'action':
            ids = set(payload['observations'])
            return {'action': payload, 'observations': [r for r in self.records if r['event_id'] in ids],
                    'effects': [child['payload'] for child in row['children']],
                    'limitations': self.projection['limitations']}
        if row['kind'] == 'effect':
            return {'effect': payload, 'snapshot': next((r for r in self.records
                        if r['event_id'] == payload['snapshot_event_id']), None)}
        return payload

    def move(self, delta):
        visible = self.visible()
        if not visible: return
        index = next((i for i, r in enumerate(visible) if r['id'] == self.selected_id), -1)
        index = max(0, min(len(visible) - 1, index + delta))
        self.selected_id = visible[index]['id']
        self.follow = False

    def expand(self, expand=True):
        row = self.selected()
        if row is None: return
        if expand:
            self.expanded.add(row['id'])
        elif row.get('parent'):
            self.selected_id = row['parent']
            self.expanded.discard(row['parent'])
        else:
            self.expanded.discard(row['id'])
        self.follow = False

    def resume(self):
        visible = self.visible()
        actionable = [r for r in visible if r['kind'] == 'action']
        selected = (actionable or visible)
        if selected: self.selected_id = selected[-1]['id']
        self.follow, self.unread = True, 0

    def set_filter(self, text):
        self.filter = text[:512]
        self.follow = False

    def toggle_journal(self):
        self.journal = not self.journal
        self.rows = build_rows(self.records, self.projection, self.journal)
        if self.follow: self.resume()

    def selection_outside(self):
        return self.selected_id is not None and not any(r['id'] == self.selected_id for r in self.visible())


class ActivityReplay:
    """One background replay; periodic reread repairs dropped UI notifications."""
    def __init__(self, directory, session):
        self.directory, self.session = directory, session
        self.pending = False
        self.results = queue.Queue(maxsize=1)
        self.last = 0
        self.dirty = True
        self.status = 'running'
        self.error = ''

    def tick(self, dirty=False, status='running'):
        self.dirty |= dirty or status != self.status
        self.status = status
        result = None
        try:
            result, self.error = self.results.get_nowait()
            self.pending = False
        except queue.Empty:
            pass
        now = time.monotonic()
        if not self.pending and ((self.dirty and now - self.last >= .1) or now - self.last >= 2):
            self.dirty, self.pending, self.last = False, True, now
            session_status = self.status
            def replay():
                try:
                    from .recorder import read_history
                    records = read_history(self.directory, self.session)
                    self.results.put(((records, project(records, session_status)), ''))
                except Exception as exc:
                    self.results.put((None, 'Activity replay unavailable: ' + str(exc)))
            threading.Thread(target=replay, name='labradour-activity', daemon=True).start()
        return result
