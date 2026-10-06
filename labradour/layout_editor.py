"""Registered layout commands; previews and disk jobs independent of curses."""
import copy
import queue
import threading

from .layout import PANES, Pane, parse_tree, preset, tree_dict
from .layout_config import load_layout


def edit_tree(tree, operation, args):
    if operation not in ('swap', 'flip', 'axis', 'ratio'):
        raise ValueError('unknown tree edit operation')
    value = tree_dict(tree)
    found = False
    if operation == 'swap':
        if len(args) != 2 or any(name not in PANES for name in args):
            raise ValueError('usage: layout swap PANE PANE')
        def swap(node):
            if node['type'] == 'pane':
                node['pane'] = args[1] if node['pane'] == args[0] else args[0] if node['pane'] == args[1] else node['pane']
            else:
                swap(node['first']); swap(node['second'])
        swap(value)
    else:
        expected = 1 if operation == 'flip' else 2
        if len(args) != expected:
            raise ValueError('usage: layout %s SPLIT_ID%s' % (operation, '' if expected == 1 else ' VALUE'))
        def edit(node):
            nonlocal found
            if node['type'] == 'pane':
                return
            if node['id'] == args[0]:
                found = True
                if operation == 'flip':
                    node['first'], node['second'] = node['second'], node['first']
                    node['ratio_bps'] = 10000 - node['ratio_bps']
                elif operation == 'axis':
                    node['axis'] = args[1]
                elif operation == 'ratio':
                    if not args[1].isascii() or not args[1].isdigit():
                        raise ValueError('ratio must be integer basis points')
                    node['ratio_bps'] = int(args[1])
            edit(node['first']); edit(node['second'])
        edit(value)
        if not found:
            raise ValueError('unknown split ID: ' + args[0])
    return parse_tree(value)


class LayoutEditor:
    def __init__(self, config):
        self.config = config
        self.open = False
        self.text = ''
        self.offset = 0
        self.preview = None
        self.message = 'Enter layout commands; Escape closes; Ctrl-Q quits'
        self.pending = False
        self.results = queue.Queue(maxsize=1)
        self.reload_options = dict(workspace=config.workspace,
            explicit=next((s['path'] for s in config.sources if s['scope'] == 'explicit'), None),
            ignore=any(s['status'] == 'skipped' for s in config.sources),
            user_file=config.paths['user'])

    def begin(self):
        self.open = True
        self.text = ''
        self.offset = 0

    def close(self):
        self.open = False
        self.text = ''
        self.preview = None

    def _disk_job(self, job, apply_tree):
        self.pending = True
        self.message = 'Layout disk operation running; input and recording continue'
        def work():
            try:
                result = job()
                self.results.put((result, apply_tree, None))
            except Exception as exc:
                self.results.put((None, False, str(exc)))
        # Disk latency cannot block the PTY/input thread. Only one job at a time.
        threading.Thread(target=work, name='labradour-layout', daemon=True).start()

    def poll(self):
        try:
            config, apply_tree, error = self.results.get_nowait()
        except queue.Empty:
            return None
        self.pending = False
        if error:
            self.message = 'Layout error: ' + error
            return None
        self.config = config
        self.message = 'Layout reloaded' if apply_tree else 'Layout saved: ' + config.active_layout
        return config, config.tree if apply_tree else None

    def execute(self, text, current):
        """Return a new applied tree or None; failed commands leave state intact."""
        try:
            words = text.split()
            if len(words) < 2 or words[0] != 'layout':
                raise ValueError('registered commands begin with layout; no shell execution')
            operation, args = words[1], words[2:]
            if self.pending:
                raise ValueError('layout disk operation still running')
            target = self.preview if self.preview is not None else current
            if operation in ('swap', 'flip', 'axis', 'ratio'):
                candidate = edit_tree(target, operation, args)
            elif operation in ('use', 'reset'):
                if len(args) != 1:
                    raise ValueError('usage: layout %s NAME' % operation)
                if operation == 'reset':
                    candidate = preset(args[0])
                else:
                    if args[0] not in self.config.layouts:
                        raise ValueError('unknown layout: ' + args[0])
                    candidate = self.config.layouts[args[0]]
            elif operation == 'preview' and not args:
                self.preview = target
                self.message = 'PREVIEW: edits do not resize Agent; layout apply or layout cancel'
                return None
            elif operation == 'apply' and not args:
                if self.preview is None:
                    raise ValueError('no preview to apply')
                candidate, self.preview = self.preview, None
                self.message = 'Preview applied'
                return candidate
            elif operation == 'cancel' and not args:
                self.preview = None
                self.message = 'Preview canceled'
                return None
            elif operation == 'status' and not args:
                self.message = 'Names: ' + ', '.join(sorted(self.config.layouts))
                return None
            elif operation == 'save':
                if self.preview is not None:
                    raise ValueError('apply or cancel preview before saving')
                flags = set(args[2:])
                if len(args) < 2 or flags - {'--confirm-replace', '--confirm-shadow'}:
                    raise ValueError('usage: layout save NAME user|workspace [--confirm-replace] [--confirm-shadow]')
                config = copy.deepcopy(self.config)
                def save():
                    config.save(args[0], args[1], tree=current,
                                confirm_replace='--confirm-replace' in flags,
                                confirm_shadow='--confirm-shadow' in flags)
                    return config
                self._disk_job(save, False)
                return None
            elif operation == 'reload' and args in ([], ['--discard']):
                if (current != self.config.tree or self.preview is not None) and args != ['--discard']:
                    raise ValueError('unsaved edits/preview; repeat layout reload --discard to discard')
                self.preview = None
                self._disk_job(lambda: load_layout(**self.reload_options), True)
                return None
            else:
                raise ValueError('unknown command or arguments')
            if self.preview is not None:
                self.preview = candidate
                self.message = 'PREVIEW updated; layout apply or layout cancel'
                return None
            self.message = 'Layout change applied'
            return candidate
        except (ValueError, OSError) as exc:
            self.message = 'Layout error: ' + str(exc)
            return None

    def input(self, kind, token, current):
        if kind in ('paste-start', 'paste-end'):
            return None
        if kind == 'key' and token in (b'\x1b', b'\x03'):
            self.close()
        elif kind == 'key' and token in (b'\x1b[A', b'\x1b[B', b'\x1b[5~', b'\x1b[6~'):
            self.offset = max(0, min(12, self.offset + (-1 if token == b'\x1b[A' else
                1 if token == b'\x1b[B' else -5 if token == b'\x1b[5~' else 5)))
        elif kind == 'key' and token in (b'\r', b'\n'):
            self.offset = 0
            text, self.text = self.text, ''
            return self.execute(text, current)
        elif kind == 'key' and token in (b'\x7f', b'\x08'):
            self.offset = 0
            self.text = self.text[:-1]
        elif kind in ('key', 'paste'):
            if kind == 'key' and token.startswith(b'\x1b'):
                return None
            self.offset = 0
            # ASCII names/commands; pasted controls never submit or close.
            self.text += ''.join(chr(b) for b in token if 32 <= b <= 126)
            self.text = self.text[:1024]
        return None

    def lines(self, current, rows, columns, focus, maximized):
        target = self.preview if self.preview is not None else current
        details = self.config.describe(rows, columns, target, focus, maximized)
        lines = ['Layout menu' + (' [PREVIEW]' if self.preview is not None else ''),
                 'Escape cancel/close | Ctrl-Q quit | Enter execute',
                 self.message, '> ' + self.text,
                 'use NAME | reset NAME | swap PANE PANE | flip ID',
                 'axis ID rows|columns | ratio ID BPS | status',
                 'preview | apply | cancel | save NAME user|workspace | reload',
                 'Focus: %s | unsaved: %s | compact: %s' % (focus, details['unsaved_changes'], details['compact_reason'])]
        def describe(node, indent=''):
            if isinstance(node, Pane):
                lines.append(indent + node.pane)
            else:
                item = next(s for s in details['splits'] if s['id'] == node.id)
                lines.append('%s%s: %s requested=%d effective=%s' % (
                    indent, node.id, node.axis, node.ratio_bps, item['effective_ratio_bps']))
                describe(node.first, indent + '  '); describe(node.second, indent + '  ')
        describe(target)
        return lines
