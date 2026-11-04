"""I2I-06: bounded installation-local transition qualification (stdlib only).

Release labels are identities, not version numbers. B can fail; C is available
and qualified. Reachability is existential recovery, NOT universal termination.
"""
from collections import deque
from dataclasses import dataclass, replace
import json
import time

LIMIT = 60000
SECONDS = 12
MUTANTS = ('owner', 'stale', 'atomic', 'barrier', 'crash_lock', 'upgrade')
IDLE = (0, 0, -1, 0)  # phase, captured current, previous, target


@dataclass(frozen=True)
class State:
    current: int = 0
    previous: int = -1
    receipt_from: int = -1
    receipt_to: int = 0
    ready: int = 1
    trial: int = 1
    serving: int = 0
    barrier: int = 0
    paused: bool = False
    operation: str = 'install'
    owners: int = 0
    workers: tuple = (IDLE, IDLE)
    transient: int = 1


def worker(s, i, value, **kw):
    values = list(s.workers)
    values[i] = value
    return replace(s, workers=tuple(values), **kw)


def edges(s, broken=None):
    for i, (phase, old, prev, target) in enumerate(s.workers):
        bit = 1 << i
        if not phase:
            # A delayed pair is evidence only, never an authority to replay a seal.
            if (old, prev) != (s.current, s.previous) and target:
                if broken == 'stale':
                    yield 'stale replay', replace(s, current=target, previous=old,
                        receipt_from=old, receipt_to=target), None
            if s.paused or (s.owners and broken != 'owner'):
                continue
            for candidate in (1, 2):
                if candidate <= s.barrier or candidate == s.current:
                    continue
                if broken == 'upgrade' and candidate == 2:
                    continue
                yield f'u{i}: acquire {candidate}', worker(s, i,
                    (1, s.current, s.previous, candidate), owners=s.owners | bit), None
            continue
        if s.transient:
            # One crash or lost acknowledgement, at any owned effect cut.
            yield f'u{i}: crash/replay', worker(s, i, IDLE, transient=0,
                owners=s.owners if broken == 'crash_lock' else s.owners & ~bit), None
        if phase == 1:
            yield f'u{i}: prepare/ready', worker(s, i, (2, old, prev, target),
                ready=s.ready | (1 << target)), None
        elif phase == 2:
            if target == 1:
                yield f'u{i}: reject B', worker(s, i, (0, old, prev, target),
                    owners=s.owners & ~bit, barrier=0 if broken == 'barrier' else 1), None
            yield f'u{i}: pinned trial PASS', worker(s, i, (3, old, prev, target),
                trial=s.trial | (1 << target)), None
        elif phase == 3:
            if (old, prev) != (s.current, s.previous):
                yield f'u{i}: refuse changed pair', worker(s, i, IDLE, owners=s.owners & ~bit), None
                continue
            yield f'u{i}: atomic seal', worker(s, i, (4, old, prev, target),
                current=target, previous=old, operation='update',
                receipt_from=s.receipt_from if broken == 'atomic' else old,
                receipt_to=s.receipt_to if broken == 'atomic' else target), None
        elif phase == 4:
            if target == 1:
                # Failed attempted start, requalified retained release, atomic rollback.
                yield f'u{i}: auto rollback B', worker(s, i, (0, old, prev, target),
                    current=old, previous=target, receipt_from=target, receipt_to=old,
                    operation='auto_rollback',
                    serving=old, barrier=0 if broken == 'barrier' else 1,
                    owners=s.owners & ~bit), None
            else:
                yield f'u{i}: drain/stop/start healthy C', worker(s, i,
                    (0, old, prev, target), serving=target, owners=s.owners & ~bit), None
    if not s.owners:
        if s.current == 2 and s.serving != 2:
            yield 'replay selected C start', replace(s, serving=2), None
        if (s.current == 1 and s.previous >= 0
                and s.operation not in ('rollback', 'auto_rollback')):
            yield 'replay B failed start/auto rollback', replace(s, current=s.previous,
                previous=1, receipt_from=1, receipt_to=s.previous, serving=s.previous,
                barrier=1, operation='auto_rollback'), None
        if s.paused or s.barrier:
            yield 'explicit resume', replace(s, paused=False, barrier=0), None
        if s.previous >= 0:
            yield 'manual rollback/pause', replace(s, current=s.previous,
                previous=s.current, receipt_from=s.current, receipt_to=s.previous,
                paused=True, operation='rollback'), None


def violation(s):
    if s.owners == 3:
        return 'single installation owner'
    if (s.receipt_from, s.receipt_to) != (s.previous, s.current):
        return 'atomic pointer/receipt agreement'
    if not (s.ready & s.trial & (1 << s.current)):
        return 'qualified selection'
    return None


def edge_violation(before, after, label):
    # Oracles do not know which mutation (if any) generated the edge.
    if label == 'stale replay':
        return 'stale-pair refusal'
    if ('reject B' in label or 'rollback B' in label) and after.barrier < 1:
        return 'rejected-candidate barrier'
    if before.current != after.current and after.receipt_from != before.current:
        return 'causal lineage'
    return violation(after)


def explore(broken=None):
    start = State()
    parents = {start: None}
    reverse = {start: []}
    queue = deque([start])
    goals = set()
    count = 0
    deadline = time.monotonic() + SECONDS

    def witness(s, last=None):
        steps = [] if last is None else [last]
        while parents[s] is not None:
            s, label = parents[s]
            steps.append(label)
        return list(reversed(steps))

    while queue:
        if len(parents) > LIMIT or time.monotonic() > deadline:
            raise RuntimeError('exploration bound exceeded; qualification incomplete')
        s = queue.popleft()
        if s.current == s.serving == 2 and not s.owners:
            goals.add(s)
        for label, nxt, bad in edges(s, broken):
            count += 1
            bad = bad or edge_violation(s, nxt, label)
            if bad:
                return {'ok': False, 'property': bad, 'witness': witness(s, label),
                        'states': len(parents), 'edges': count}
            if nxt not in parents:
                parents[nxt] = (s, label)
                reverse[nxt] = []
                queue.append(nxt)
            reverse[nxt].append(s)
    # Every explored prefix must have SOME finite continuation to healthy successor C.
    reachable = set(goals)
    queue = deque(goals)
    while queue:
        if time.monotonic() > deadline:
            raise RuntimeError('exploration bound exceeded; qualification incomplete')
        for s in reverse[queue.popleft()]:
            if s not in reachable:
                reachable.add(s)
                queue.append(s)
    missing = next((s for s in parents if s not in reachable), None)
    return {'ok': missing is None, 'property': 'healthy successor recovery reachability',
            'states': len(parents), 'edges': count, 'goals': len(goals),
            'witness': [] if missing is None else witness(missing)}


def main():
    result = {'work': 'I2I-06 / issue26', 'bounds': {'releases': 3, 'updaters': 2,
        'transient_crashes_or_ack_losses': 1, 'state_cap': LIMIT, 'seconds_per_run': SECONDS},
        'assumptions': ['available IO/task/qualified C', 'finite drain',
                        'scheduler and owner permit recovery and explicit install/rollback/resume',
                        'existential continuation; no universal or permanent-failure proof'],
        'baseline': explore(), 'negative_controls': {name: explore(name) for name in MUTANTS}}
    print(json.dumps(result, sort_keys=True, separators=(',', ':')))
    return 0 if result['baseline']['ok'] and all(not v['ok'] for v in result['negative_controls'].values()) else 1


if __name__ == '__main__':
    raise SystemExit(main())
