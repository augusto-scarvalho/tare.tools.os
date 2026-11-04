"""AGY researcher boundary hook. Standalone stdlib file, no project imports."""
import json
from pathlib import Path
import sys


def main():
    path, phase = Path(sys.argv[1]), sys.argv[2]
    maximum = None if sys.argv[3] == 'unlimited' else int(sys.argv[3])
    event = json.loads(sys.stdin.buffer.read(65536))
    rows = [json.loads(line) for line in path.read_bytes().splitlines()] if path.exists() else []
    name = event.get('toolCall', {}).get('name')
    allowed = name in {'search_web', 'read_url_content', 'finish'}
    with path.open('a', encoding='utf-8') as stream:
        stream.write(json.dumps({'phase': phase, 'tool': name, 'model': event.get('modelName'),
            'allowed': allowed if phase == 'tool' else None})+'\n')
    if phase == 'tool':
        result = {'decision': 'allow' if allowed else 'deny', 'reason': 'This role only researches public web sources.'}
    elif phase == 'pre' and maximum is not None and sum(r['phase'] == 'pre' for r in rows) >= maximum:
        result = {'terminationBehavior': 'terminate'}
    else:
        # A post hook runs before the CLI serializes its final structured
        # handoff. Killing at the ceiling discarded an already produced report.
        # The pre hook prevents another generation while allowing that handoff.
        result = {}
    print(json.dumps(result))


if __name__ == '__main__':
    main()
