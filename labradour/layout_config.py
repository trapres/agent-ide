"""Bounded declarative layout layers and explicit, conflict-aware persistence."""
from dataclasses import dataclass, field
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import tempfile

from .layout import PANES, PRESETS, Pane, Split, arrange, compact_reason, pane_order, parse_tree, preset, tree_dict

MAX_BYTES = 64 * 1024
NAME = re.compile(r'[A-Za-z][A-Za-z0-9_-]{0,31}\Z')


def check_name(name):
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise ValueError('layout names must start with a letter and contain 1-32 ASCII letters/digits/_/-')
    return name


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError('duplicate JSON key: ' + key)
        result[key] = value
    return result


def decode(data):
    if len(data) > MAX_BYTES:
        raise ValueError('layout config exceeds 64 KiB')
    try:
        obj = json.loads(data.decode('utf-8'), object_pairs_hook=_pairs,
                         parse_constant=lambda _: (_ for _ in ()).throw(ValueError('non-finite JSON number')))
    except (UnicodeError, RecursionError) as exc:
        raise ValueError('invalid UTF-8 or excessive JSON nesting') from exc
    if not isinstance(obj, dict) or set(obj) - {'schema_version', 'layouts', 'active_layout', 'initial_focus'}:
        raise ValueError('invalid layout configuration fields')
    if type(obj.get('schema_version')) is not int or obj['schema_version'] != 1:
        raise ValueError('unsupported layout schema_version; original file must be preserved')
    layouts = obj.get('layouts', {})
    if not isinstance(layouts, dict) or len(layouts) > 32:
        raise ValueError('layouts must be an object with at most 32 definitions')
    for name, tree in layouts.items():
        check_name(name)
        parse_tree(tree)
    if 'active_layout' in obj:
        check_name(obj['active_layout'])
    if 'initial_focus' in obj and (not isinstance(obj['initial_focus'], str) or obj['initial_focus'] not in PANES):
        raise ValueError('initial_focus must name a known pane')
    return obj


def _safe_parents(path):
    # Refuse redirecting the config directory itself. Higher-level locations
    # may legitimately use system aliases such as macOS /tmp -> /private/tmp.
    parent = path.parent
    try:
        mode = parent.lstat().st_mode
    except FileNotFoundError:
        return
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        raise ValueError('layout parent must be a real directory: ' + str(parent))


def read_file(path):
    """Return bounded bytes and an identity/content stamp, or (None, None)."""
    path = Path(path)
    _safe_parents(path)
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None, None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError('layout file must be regular: ' + str(path))
        if info.st_size > MAX_BYTES:
            raise ValueError('layout config exceeds 64 KiB: ' + str(path))
        data = b''
        while len(data) <= MAX_BYTES:
            chunk = os.read(fd, MAX_BYTES + 1 - len(data))
            if not chunk:
                break
            data += chunk
        if len(data) > MAX_BYTES:
            raise ValueError('layout config exceeds 64 KiB: ' + str(path))
        after = os.fstat(fd)
        if (info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ValueError('layout file changed while reading: ' + str(path))
        stamp = (after.st_dev, after.st_ino, after.st_mtime_ns, after.st_ctime_ns,
                 hashlib.sha256(data).hexdigest())
        return data, stamp
    finally:
        os.close(fd)


def user_path():
    xdg = os.environ.get('XDG_CONFIG_HOME', '')
    root = Path(xdg) if xdg and Path(xdg).is_absolute() else Path.home() / '.config'
    # abspath retains symlink names so read/save can refuse them.
    return Path(os.path.abspath(str(root / 'labradour/layout.json')))


def legacy_tree(side=None, agent_width=None, activity_height=None):
    ratios = []
    for value in (agent_width, activity_height):
        value = .5 if value is None else value
        if not math.isfinite(value):
            raise ValueError('legacy layout ratios must be finite')
        ratios.append(round(min(.7, max(.3, value)) * 10000))
    review = Split('review', 'rows', ratios[1], Pane('activity'), Pane('visualization'))
    if side == 'right':
        return Split('main', 'columns', 10000 - ratios[0], review, Pane('agent'))
    return Split('main', 'columns', ratios[0], Pane('agent'), review)


@dataclass
class LayoutConfig:
    workspace: Path
    paths: dict
    layouts: dict
    active_layout: str = 'default'
    initial_focus: str = 'agent'
    sources: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    snapshots: dict = field(default_factory=dict)
    documents: dict = field(default_factory=dict)
    ignored: set = field(default_factory=set)
    tree: object = None

    def describe(self, rows=40, columns=140, tree=None, focus=None, maximized=False):
        tree = self.tree if tree is None else tree
        focus = self.initial_focus if focus is None else focus
        rects = arrange(tree, rows, columns, focus, maximized)
        splits = []

        def describe_split(node):
            if isinstance(node, Pane):
                return
            item = {'id': node.id, 'axis': node.axis, 'requested_ratio_bps': node.ratio_bps,
                    'first_extent': None, 'total_extent': None, 'effective_ratio_bps': None}
            if len(rects) == 3:
                def extent(child):
                    leaves = [rects[name] for name in pane_order(child)]
                    return (max(r.y + r.height for r in leaves) - min(r.y for r in leaves)
                            if node.axis == 'rows' else
                            max(r.x + r.width for r in leaves) - min(r.x for r in leaves))
                first, second = extent(node.first), extent(node.second)
                item.update(first_extent=first, total_extent=first + second,
                            effective_ratio_bps=round(first * 10000 / (first + second)))
            splits.append(item)
            describe_split(node.first)
            describe_split(node.second)

        describe_split(tree)
        return {'schema_version': 1, 'active_layout': self.active_layout,
                'initial_focus': self.initial_focus, 'available_layouts': sorted(self.layouts),
                'sources': self.sources, 'warnings': self.warnings, 'tree': tree_dict(tree),
                'unsaved_changes': tree != self.tree,
                'viewport': {'rows': rows, 'columns': columns},
                'focus': focus, 'maximized': maximized, 'splits': splits,
                'compact_reason': compact_reason(tree, rows, columns),
                'geometry': {name: dict(y=r.y, x=r.x, height=r.height, width=r.width,
                                       content_rows=r.content_size[0], content_columns=r.content_size[1])
                             for name, r in rects.items()},
                'save_paths': {scope: str(path) for scope, path in self.paths.items()}}

    def save(self, name, scope='user', tree=None, confirm_replace=False, confirm_shadow=False):
        check_name(name)
        if scope not in self.paths:
            raise ValueError('save scope must be user or workspace')
        tree = parse_tree(tree_dict(self.tree if tree is None else tree))
        path = self.paths[scope]
        _safe_parents(path)
        current, stamp = read_file(path)
        if scope in self.snapshots and stamp != self.snapshots[scope]:
            raise ValueError('layout destination changed; reload before saving: ' + str(path))
        ignored = scope in self.ignored
        try:
            document = decode(current) if current is not None else {'schema_version': 1}
        except ValueError:
            # Unsupported versions cannot be overwritten even after confirmation.
            try:
                raw = json.loads(current)
            except (ValueError, RecursionError):
                raw = None
            if isinstance(raw, dict) and 'schema_version' in raw and (type(raw['schema_version']) is not int or raw['schema_version'] != 1):
                raise ValueError('unsupported schema version; preserve original file and use another destination')
            if not confirm_replace:
                raise ValueError('invalid destination; explicit --confirm-replace required')
            document = {'schema_version': 1}
        if ignored and current is not None and not confirm_replace:
            raise ValueError('ignored destination; explicit --confirm-replace required')
        existing = document.get('layouts', {})
        if name in self.layouts and name not in existing and not confirm_shadow:
            raise ValueError('save shadows an inherited layout; explicit --confirm-shadow required')
        document = dict(document, layouts=dict(existing, **{name: tree_dict(tree)}),
                        active_layout=name, initial_focus=self.initial_focus)
        data = (json.dumps(document, indent=2, ensure_ascii=True) + '\n').encode('utf-8')
        decode(data)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        _safe_parents(path)
        lock_fd = os.open(str(path.with_name('.layout.json.lock')), os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        temporary = None
        try:
            if not stat.S_ISREG(os.fstat(lock_fd).st_mode):
                raise ValueError('layout save lock must be regular')
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ValueError('another layout save is active; retry') from exc
            if read_file(path)[1] != stamp:
                raise ValueError('layout destination changed; reload before saving')
            fd, temporary = tempfile.mkstemp(prefix='.layout-', dir=str(path.parent))
            with os.fdopen(fd, 'wb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            # Catch changes during staging; external writers must cooperate with
            # the lock to exclude the final check/replace race entirely.
            if read_file(path)[1] != stamp:
                raise ValueError('layout destination changed during save; reload')
            os.replace(temporary, path)
            temporary = None
            directory = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if temporary is not None:
                os.unlink(temporary)
            os.close(lock_fd)
        self.snapshots[scope] = read_file(path)[1]
        self.documents[scope] = document
        self.ignored.discard(scope)
        self.layouts[name] = tree
        self.active_layout, self.tree = name, tree
        self.sources.append({'scope': scope, 'path': str(path), 'status': 'saved', 'active_layout': name})
        return {'path': str(path), 'active_layout': name, 'scope': scope}


def load_layout(workspace, explicit=None, name=None, ignore=False, side=None,
                agent_width=None, activity_height=None, user_file=None):
    if ignore and explicit is not None:
        raise ValueError('--ignore-layout-config cannot be combined with --layout-config')
    legacy = any(value is not None for value in (side, agent_width, activity_height))
    if name is not None and legacy:
        raise ValueError('--layout cannot be combined with --side, --agent-width or --activity-height')
    workspace = Path(workspace).resolve()
    if not workspace.is_dir():
        raise ValueError('layout workspace must be an existing directory')
    paths = {'user': Path(user_file) if user_file is not None else user_path(),
             'workspace': workspace / '.labradour/layout.json'}
    result = LayoutConfig(workspace, paths, {key: preset(key) for key in PRESETS})
    layers = list(paths.items())
    if explicit is not None:
        layers.append(('explicit', Path(os.path.abspath(str(explicit)))))
    active_scope = None
    for scope, path in layers:
        source = {'scope': scope, 'path': str(path)}
        if ignore:
            result.ignored.add(scope)
            source['status'] = 'skipped'
            result.sources.append(source)
            continue
        try:
            data, stamp = read_file(path)
            result.snapshots[scope] = stamp
            if data is None:
                if scope == 'explicit':
                    raise ValueError('explicit layout file does not exist')
                source['status'] = 'absent'
                result.sources.append(source)
                continue
            document = decode(data)
        except (OSError, ValueError) as exc:
            if scope == 'explicit':
                raise ValueError(str(path) + ': ' + str(exc)) from exc
            source.update(status='ignored', reason=str(exc))
            result.ignored.add(scope)
            result.warnings.append(str(path) + ': ' + str(exc))
            result.sources.append(source)
            continue
        result.documents[scope] = document
        result.layouts.update({key: parse_tree(value) for key, value in document.get('layouts', {}).items()})
        if 'active_layout' in document:
            result.active_layout = document['active_layout']
            active_scope = scope
        result.initial_focus = document.get('initial_focus', result.initial_focus)
        source.update(status='loaded', layouts=sorted(document.get('layouts', {})))
        for preference in ('active_layout', 'initial_focus'):
            if preference in document:
                source[preference] = document[preference]
        result.sources.append(source)
    if name is not None:
        result.active_layout = check_name(name)
        active_scope = 'requested'
    if legacy:
        result.tree = legacy_tree(side, agent_width, activity_height)
        result.active_layout = 'legacy'
        result.sources.append({'scope': 'launch', 'status': 'legacy', 'active_layout': 'legacy'})
    else:
        if result.active_layout not in result.layouts:
            if active_scope in ('explicit', 'requested'):
                raise ValueError('unknown requested layout: ' + result.active_layout)
            result.warnings.append('unknown discovered layout: ' + result.active_layout + '; using built-in default')
            result.active_layout = 'default'
            result.tree = preset()
        else:
            result.tree = result.layouts[result.active_layout]
        if name is not None:
            result.sources.append({'scope': 'launch', 'status': 'selected', 'active_layout': name})
    return result
