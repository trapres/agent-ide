"""Compare an idle, fixed-size private native terminal trace with tmux text cells."""
import argparse
import json
from pathlib import Path
import shlex
import subprocess
import time
import uuid


def compare(root):
    root=Path(root).resolve()
    expected=json.loads((root/'native-terminal.json').read_text())
    replay=root/'oracle-replay.bin'
    replay.write_bytes((root/'native-terminal.bin').read_bytes())
    replay.chmod(0o600)
    config=root/'oracle-tmux.conf'
    config.write_text('set -g status off\n')
    server='labradour-oracle-'+uuid.uuid4().hex
    command=['tmux','-L',server,'-f',str(config)]
    try:
        subprocess.run([*command,'new-session','-d','-x',str(expected['columns']),'-y',str(expected['rows']),
                        '-s','oracle','stty -echo; cat '+shlex.quote(str(replay))+'; sleep 30'],check=True)
        # Wait only for a short local replay, not native model execution.
        previous=None; stable=0
        for _ in range(50):
            actual=subprocess.check_output([*command,'capture-pane','-p','-t','oracle']).decode().splitlines()
            stable=stable+1 if actual==previous else 0
            if stable>=5:break
            previous=actual; time.sleep(.1)
        differences=[i for i in range(expected['rows'])
                     if expected['display'][i].rstrip() != (actual[i] if i<len(actual) else '').rstrip()]
        result={'scope':'Fixed-size native text cells only; excludes colors, outer curses rendering, resize and human visual acceptance.',
                'rows':expected['rows'],'columns':expected['columns'],'mismatch_rows':differences,
                'match':not differences,'trace_bytes':replay.stat().st_size}
        (root/'terminal-oracle.json').write_text(json.dumps(result,indent=2)+'\n')
        return result
    finally:
        subprocess.run([*command,'kill-server'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root',type=Path)
    print(json.dumps(compare(parser.parse_args().root),indent=2))
