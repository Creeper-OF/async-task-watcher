#!/usr/bin/env python3
"""Durable finite Linux/WSL jobs, using pidfd exit events rather than polling."""
import argparse
import contextlib
import ctypes
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import sys
import time
import uuid

if sys.platform != 'linux' or not hasattr(os, 'pidfd_open'):
    raise SystemExit('Use native tool waiting: this supervisor requires Linux pidfd (WSL2 supported).')
import fcntl

TERMINAL = {'completed', 'failed', 'timed_out', 'cancelled'}
BOOT = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
SCRIPT = str(Path(__file__).resolve())
LOG_BYTES = 16384
GRACE = 0.5


def now():
    return datetime.now(timezone.utc).isoformat()


def positive(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError('must be a finite positive number')
    return number


def identity(pid):
    try:
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        return {'pid': int(pid), 'start_ticks': int(fields[19]), 'boot_id': BOOT}
    except (FileNotFoundError, ProcessLookupError):
        return None


def pidfd(ident):
    if not ident or ident.get('boot_id') != BOOT or identity(ident['pid']) != ident:
        return None
    try:
        fd = os.pidfd_open(ident['pid'])
    except ProcessLookupError:
        return None
    if identity(ident['pid']) != ident:
        os.close(fd)
        return None
    return fd


def send_signal(ident, sig):
    fd = pidfd(ident)
    if fd is None:
        return False
    try:
        signal.pidfd_send_signal(fd, sig)
        return True
    except ProcessLookupError:
        return False
    finally:
        os.close(fd)


def block_exit(fd, timeout=None):
    with selectors.DefaultSelector() as sel:
        sel.register(fd, selectors.EVENT_READ)
        return bool(sel.select(timeout))


def prctl(option, value):
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(option, value, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), 'prctl failed')


def parent_death(parent_pid):
    # Executed in the single-threaded subprocess child before exec.
    prctl(1, signal.SIGKILL)  # PR_SET_PDEATHSIG
    if os.getppid() != parent_pid:
        os.kill(os.getpid(), signal.SIGKILL)


def atomic_json(path, value):
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(value, f, ensure_ascii=False, sort_keys=True)
            f.write('\n')
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temp.unlink(missing_ok=True)


@contextlib.contextmanager
def locked(path):
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def read_state(directory):
    return json.loads((directory / 'state.json').read_text())


def change(directory, transform):
    with locked(directory / 'state.lock'):
        state = read_state(directory)
        transform(state)
        atomic_json(directory / 'state.json', state)
        return state


def patch(directory, **values):
    return change(directory, lambda state: state.update(values))


def finish(directory, status, exit_code, error=None, **values):
    def update(state):
        if state['status'] not in TERMINAL:
            state.update(status=status, exit_code=exit_code, error=error, finished_at=now(),
                         timed_out=status == 'timed_out', **values)
    return change(directory, update)


def task_dir(root, task_id):
    if not re.fullmatch(r'[0-9a-f]{32}', task_id):
        raise ValueError('invalid task ID')
    directory = root / task_id
    if directory.is_symlink() or not (directory / 'state.json').is_file():
        raise ValueError('task not found or unsafe task directory')
    return directory


def ensure_root(root):
    if root.is_symlink() or root in {Path('/'), Path.home(), Path.cwd()}:
        raise ValueError('unsafe task root')
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Lock the directory inode before the marker exists; a lock file itself would
    # make the first-time directory look nonempty to a concurrent starter.
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if os.fstat(fd).st_uid != os.getuid():
            raise ValueError('task root must be owned by the current user')
        fcntl.flock(fd, fcntl.LOCK_EX)
        marker = root / '.async-task-watcher'
        if not marker.exists() and any(root.iterdir()):
            raise ValueError('refusing to use a nonempty, unrelated directory as task root')
        marker_fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        os.close(marker_fd)
        os.fchmod(fd, 0o700)
    finally:
        os.close(fd)


def group_members(pgid):
    members = []
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit():
            continue
        try:
            fields = (proc / 'stat').read_text().rsplit(')', 1)[1].split()
            if int(fields[2]) == pgid and fields[0] != 'Z':
                ident = identity(int(proc.name))
                if ident:
                    members.append(ident)
        except (FileNotFoundError, ProcessLookupError):
            pass
    return members


def cleanup_group(leader):
    """Leader is not reaped until cleanup: its PID reserves the process-group ID."""
    if not leader or identity(leader['pid']) != leader:
        return False
    pgid = leader['pid']
    handles = []
    try:
        for member in group_members(pgid):
            fd = pidfd(member)
            if fd is not None:
                handles.append(fd)
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            return True
        with selectors.DefaultSelector() as sel:
            for fd in handles:
                sel.register(fd, selectors.EVENT_READ)
            end = time.monotonic() + GRACE
            while sel.get_map():
                events = sel.select(max(0, end - time.monotonic()))
                if not events:
                    break
                for key, _ in events:
                    sel.unregister(key.fd)
            # One reconciliation snapshot also catches children forked during TERM.
            if group_members(pgid):
                os.killpg(pgid, signal.SIGKILL)
                for key in list(sel.get_map().values()):
                    block_exit(key.fd, 2)
        return not group_members(pgid)
    except ProcessLookupError:
        return True
    finally:
        for fd in handles:
            os.close(fd)


def ready(fd):
    if fd is not None:
        try:
            os.write(fd, b'R')
        except BrokenPipeError:
            pass  # Client interruption does not cancel a launched job.
        finally:
            os.close(fd)


def guardian(directory, parent_pid, ready_fd):
    os.umask(0o077)
    patch(directory, guardian_pid=os.getpid(), guardian_identity=identity(os.getpid()))
    parent = pidfd(identity(parent_pid))
    job = None
    pipe_r, pipe_w = os.pipe2(os.O_NONBLOCK | os.O_CLOEXEC)
    cancelled = False
    def on_cancel(_sig, _frame):
        nonlocal cancelled
        cancelled = True
    signal.signal(signal.SIGTERM, on_cancel)
    signal.signal(signal.SIGINT, on_cancel)
    signal.set_wakeup_fd(pipe_w)
    try:
        state = read_state(directory)
        if state['status'] in TERMINAL:
            ready(ready_fd); ready_fd = None
            return
        if parent is None or cancelled or state['cancel_requested']:
            finish(directory, 'failed' if parent is None else 'cancelled', None,
                   'supervisor exited before launch' if parent is None else None)
            ready(ready_fd); ready_fd = None
            return
        guardian_pid = os.getpid()
        with open(state['stdout_log'], 'ab', buffering=0) as out, open(state['stderr_log'], 'ab', buffering=0) as err:
            job = subprocess.Popen(state['command'], cwd=state['cwd'], stdin=subprocess.DEVNULL,
                                   stdout=out, stderr=err, start_new_session=True,
                                   preexec_fn=lambda: parent_death(guardian_pid))
        leader = identity(job.pid)
        patch(directory, pid=job.pid, job_identity=leader, started_at=now(), status='running',
              deadline=time.time() + state['timeout_seconds'])
        ready(ready_fd); ready_fd = None
        job_fd = pidfd(leader)
        if job_fd is None:
            raise RuntimeError('unable to subscribe to command exit')
        try:
            with selectors.DefaultSelector() as sel:
                sel.register(job_fd, selectors.EVENT_READ, 'job')
                sel.register(parent, selectors.EVENT_READ, 'parent')
                sel.register(pipe_r, selectors.EVENT_READ, 'cancel')
                end = time.monotonic() + state['timeout_seconds']
                events = sel.select(max(0, end - time.monotonic()))
                labels = {k.data for k, _ in events}
                if 'parent' in labels:
                    status, error = 'failed', 'supervisor exited unexpectedly; guardian cleaned task'
                elif cancelled or 'cancel' in labels or read_state(directory)['cancel_requested']:
                    status, error = 'cancelled', None
                elif not events:
                    status, error = 'timed_out', None
                else:
                    status, error = 'completed', None
                cleaned = cleanup_group(leader)
                code = job.wait()
                if status == 'completed' and code != 0:
                    status = 'failed'
                finish(directory, status, code, error, cleanup_complete=cleaned,
                       cancel_requested=status == 'cancelled' or read_state(directory)['cancel_requested'])
        finally:
            os.close(job_fd)
    except BaseException as exc:
        if job and job.returncode is None:
            cleanup_group(identity(job.pid))
            job.wait()
        finish(directory, 'failed', None, f'{type(exc).__name__}: {exc}')
    finally:
        ready(ready_fd)
        signal.set_wakeup_fd(-1)
        os.close(pipe_r); os.close(pipe_w)
        if parent is not None:
            os.close(parent)


def reap_adopted():
    while True:
        try:
            if os.waitpid(-1, os.WNOHANG)[0] == 0:
                break
        except ChildProcessError:
            break


def notify_task(directory):
    claimed = False
    def claim(state):
        nonlocal claimed
        notice = state['notification']
        if state['status'] not in TERMINAL:
            raise ValueError('cannot notify before completion')
        if notice['status'] != 'pending':
            return
        notice.update(status='sending', attempted_at=now(), attempts=1,
                      owner_identity=identity(os.getpid()))
        claimed = True
    state = change(directory, claim)
    if not claimed:
        return state
    proc = None
    notice = dict(state['notification'])
    try:
        env = dict(os.environ, ASYNC_TASK_ID=state['task_id'], ASYNC_TASK_DIR=str(directory))
        parent_pid = os.getpid()
        with open(directory / 'notification.log', 'ab', buffering=0) as log:
            proc = subprocess.Popen(notice['argv'], cwd=state['cwd'], env=env, stdin=subprocess.DEVNULL,
                                    stdout=log, stderr=log, start_new_session=True,
                                    preexec_fn=lambda: parent_death(parent_pid))
            leader = identity(proc.pid)
            fd = pidfd(leader)
            if fd is None:
                raise RuntimeError('cannot subscribe to notifier exit')
            try:
                done = block_exit(fd, 10)
            finally:
                os.close(fd)
            cleaned = cleanup_group(leader)
            code = proc.wait()
            notice.update(status='delivered' if done and code == 0 and cleaned else 'failed', exit_code=code,
                          cleanup_complete=cleaned,
                          error='notification timeout' if not done else None if cleaned else 'notification cleanup incomplete')
    except Exception as exc:
        if proc and proc.returncode is None:
            cleanup_group(identity(proc.pid)); proc.wait()
        notice.update(status='failed', exit_code=None, error=f'{type(exc).__name__}: {exc}')
    notice.update(finished_at=now(), consumed=None)
    return patch(directory, notification=notice)


def supervise(directory, ready_fd):
    os.umask(0o077)
    prctl(36, 1)  # PR_SET_CHILD_SUBREAPER: retain identities of orphaned command children.
    patch(directory, supervisor_pid=os.getpid(), supervisor_identity=identity(os.getpid()))
    guard = None
    def cancel_guard(_sig, _frame):
        if guard is not None:
            try:
                os.kill(guard.pid, signal.SIGTERM)  # It is our unreaped direct child.
            except ProcessLookupError:
                pass
    signal.signal(signal.SIGTERM, cancel_guard)
    signal.signal(signal.SIGINT, cancel_guard)
    try:
        if read_state(directory)['status'] in TERMINAL:
            ready(ready_fd); ready_fd = None
            return
        guard = subprocess.Popen([sys.executable, SCRIPT, '_guardian', str(directory),
                                  str(os.getpid()), str(ready_fd)], pass_fds=(ready_fd,),
                                 stdin=subprocess.DEVNULL)
        os.close(ready_fd); ready_fd = None
        guard.wait()  # Blocking waitpid: no timeout loop or model polling.
        state = read_state(directory)
        if state['status'] not in TERMINAL:
            cleaned = cleanup_group(state.get('job_identity'))
            finish(directory, 'failed', None, f'guardian exited unexpectedly ({guard.returncode})',
                   cleanup_complete=cleaned)
        reap_adopted()
        notify_task(directory)
    except BaseException as exc:
        if guard and guard.returncode is None:
            send_signal(identity(guard.pid), signal.SIGTERM)
            guard.wait()
        finish(directory, 'failed', None, f'supervisor error: {exc}')
    finally:
        ready(ready_fd)


def start(root, args):
    ensure_root(root)
    argv = args.command[1:] if args.command[:1] == ['--'] else args.command
    if not argv or not all(isinstance(x, str) and '\x00' not in x for x in argv):
        raise ValueError('provide a nonempty argv after --')
    if argv[0].lower().endswith('.exe'):
        raise ValueError('use native Windows execution/waiting for .exe process trees')
    cwd = str(Path(args.cwd).expanduser().resolve(strict=True))
    if not Path(cwd).is_dir():
        raise ValueError('cwd must be a directory')
    notice = json.loads(args.notify_json) if args.notify_json else None
    if notice is not None and (not isinstance(notice, list) or not notice or
                               not all(isinstance(x, str) and '\x00' not in x for x in notice)):
        raise ValueError('notify-json must be a nonempty argv array')
    spec = {'command': argv, 'cwd': cwd, 'timeout_seconds': args.timeout,
            'context': args.context, 'thread_id': args.thread, 'notify': notice}
    signature = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()
    task_id = hashlib.sha256((args.thread + '\0' + args.key).encode()).hexdigest()[:32] if args.key else uuid.uuid4().hex
    directory = root / task_id
    with locked(root / 'start.lock'):
        if directory.exists():
            old = read_state(task_dir(root, task_id))
            if old['signature'] != signature:
                raise ValueError('key already exists with different command, cwd, timeout or context')
            return old
        directory.mkdir(mode=0o700)
        for name in ('stdout.log', 'stderr.log', 'supervisor.log'):
            fd = os.open(directory / name, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
        state = {**spec, 'signature': signature, 'task_id': task_id, 'status': 'pending',
                 'created_at': now(), 'started_at': None, 'finished_at': None, 'exit_code': None,
                 'deadline': time.time() + args.timeout, 'pid': None, 'supervisor_pid': None,
                 'guardian_pid': None, 'supervisor_identity': None, 'guardian_identity': None,
                 'job_identity': None, 'cancel_requested': False, 'timed_out': False, 'error': None,
                 'cleanup_complete': None, 'acknowledgements': {},
                 'stdout_log': str(directory / 'stdout.log'), 'stderr_log': str(directory / 'stderr.log'),
                 'notification': {'argv': notice, 'status': 'pending' if notice else 'disabled',
                                  'attempts': 0, 'exit_code': None, 'consumed': None}}
        state.pop('notify')
        atomic_json(directory / 'state.json', state)
        read_fd, write_fd = os.pipe2(os.O_CLOEXEC)
        try:
            with open(directory / 'supervisor.log', 'ab', buffering=0) as log:
                proc = subprocess.Popen([sys.executable, SCRIPT, '_supervise', str(directory), str(write_fd)],
                                        pass_fds=(write_fd,), start_new_session=True, stdin=subprocess.DEVNULL,
                                        stdout=log, stderr=log)
            os.close(write_fd); write_fd = None
            with selectors.DefaultSelector() as sel:
                sel.register(read_fd, selectors.EVENT_READ)
                if not sel.select(8):
                    # State is durable; no automatic duplicate execution on uncertain startup.
                    send_signal(identity(proc.pid), signal.SIGTERM)
                    raise RuntimeError(f'startup handshake timed out; recover task {task_id}')
                os.read(read_fd, 1)
            return read_state(directory)
        finally:
            os.close(read_fd)
            if write_fd is not None:
                os.close(write_fd)


def recover(directory):
    if read_state(directory)['status'] == 'pending':
        # The starter owns this lock until its launch handshake commits.
        with locked(directory.parent / 'start.lock'):
            return recover_unlocked(directory)
    return recover_unlocked(directory)


def recover_unlocked(directory):
    state = read_state(directory)
    if state['status'] in TERMINAL:
        notice = state['notification']
        if notice['status'] == 'sending':
            fd = pidfd(notice.get('owner_identity'))
            try:
                gone = fd is None or block_exit(fd, 0)
            finally:
                if fd is not None:
                    os.close(fd)
            if gone:
                def interrupted(current):
                    current_notice = current['notification']
                    if current_notice['status'] == 'sending' and current_notice.get('owner_identity') == notice.get('owner_identity'):
                        current_notice.update(status='failed', error='notification sender interrupted; consumption unknown',
                                              exit_code=None, finished_at=now(), consumed=None)
                state = change(directory, interrupted)
        return state
    for name in ('supervisor_identity', 'guardian_identity'):
        fd = pidfd(state.get(name))
        if fd is not None:
            try:
                if not block_exit(fd, 0):
                    return state
            finally:
                os.close(fd)
    cleaned = cleanup_group(state.get('job_identity'))
    return finish(directory, 'failed', None, 'supervisor/guardian unavailable; result unknown, not rerun',
                  cleanup_complete=cleaned)


def wait_task(directory, timeout=None):
    end = time.monotonic() + timeout if timeout else None
    # A waiter can attach during the start transaction before the PID fields are
    # committed. Resolve that transaction first, then subscribe to its process.
    recover(directory)
    for role in ('supervisor_identity', 'guardian_identity'):
        state = read_state(directory)
        fd = pidfd(state.get(role))
        if fd is None:
            continue
        try:
            budget = max(0, end - time.monotonic()) if end else max(0, state['deadline'] - time.time()) + 16
            if not block_exit(fd, budget):
                if timeout:
                    return {**read_state(directory), 'wait_expired': True}
                # A broken supervisor must not leave an unbounded wait. Stop its owned guard.
                cancel_task(directory)
                raise RuntimeError('task safety deadline elapsed without supervisor completion; cancellation requested')
        finally:
            os.close(fd)
    return recover(directory)


def cancel_task(directory):
    def cancel(state):
        if state['status'] not in TERMINAL:
            state['cancel_requested'] = True
    state = change(directory, cancel)
    if state['status'] not in TERMINAL:
        for role in ('guardian_identity', 'supervisor_identity'):
            if send_signal(state.get(role), signal.SIGTERM):
                break
        state = recover(directory)
    return state


def tail(path, limit=LOG_BYTES):
    try:
        with open(path, 'rb') as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - limit))
            return f.read(limit).decode('utf-8', errors='replace')
    except FileNotFoundError:
        return ''


def summary(state):
    return {**state, 'stdout_tail': tail(state['stdout_log']), 'stderr_tail': tail(state['stderr_log'])}


def exit_code(state):
    if state.get('wait_expired'):
        return 75
    if state['status'] == 'timed_out':
        return 124
    if state['status'] == 'cancelled':
        return 130
    if state['status'] == 'completed':
        return 0
    code = state['exit_code']
    if state['error'] or code is None:
        return 70
    return min(255, code if code > 0 else 128 - code)


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root', default=str(Path(os.environ.get('XDG_STATE_HOME', str(Path.home() / '.local/state'))) / 'async-task-watcher'))
    sub = p.add_subparsers(dest='action', required=True)
    for action in ('start', 'run'):
        s = sub.add_parser(action, help='finite argv job; start detaches, run waits and returns job exit status')
        s.add_argument('--cwd', default=os.getcwd())
        s.add_argument('--timeout', required=True, type=positive, help='job safety deadline in seconds')
        s.add_argument('--context', default='')
        s.add_argument('--thread', default=os.environ.get('CODEX_THREAD_ID', ''))
        s.add_argument('--key', help='optional per-thread idempotency key; reuse requires identical specification')
        s.add_argument('--notify-json', help='optional authorized argv callback; no automatic Codex wake promise')
        s.add_argument('command', nargs=argparse.REMAINDER)
    for action in ('wait', 'status', 'cancel', 'logs', 'ack', 'notify'):
        s = sub.add_parser(action)
        s.add_argument('task_id')
        if action == 'wait':
            s.add_argument('--timeout', type=positive, help='wait budget only; expiry does not cancel job')
        if action == 'ack':
            s.add_argument('--consumer', default=os.environ.get('CODEX_THREAD_ID', 'agent'))
    s = sub.add_parser('list')
    s.add_argument('--cwd')
    s.add_argument('--thread')
    s.add_argument('--unhandled', action='store_true')
    return p


def main():
    if len(sys.argv) > 1 and sys.argv[1] in ('_supervise', '_guardian'):
        directory = Path(sys.argv[2])
        if sys.argv[1] == '_supervise':
            supervise(directory, int(sys.argv[3]))
        else:
            guardian(directory, int(sys.argv[3]), int(sys.argv[4]))
        return 0
    args = parser().parse_args()
    root = Path(args.root).expanduser().absolute()
    try:
        result, code = None, 0
        if args.action in ('start', 'run'):
            result = start(root, args)
            if args.action == 'run':
                result = wait_task(task_dir(root, result['task_id']))
                code = exit_code(result)
            result = summary(result)
        elif args.action == 'list':
            tasks = []
            if root.exists():
                for item in sorted(root.iterdir()):
                    if re.fullmatch(r'[0-9a-f]{32}', item.name) and not item.is_symlink():
                        directory = task_dir(root, item.name)
                        state = read_state(directory)
                        if args.cwd and state['cwd'] != str(Path(args.cwd).resolve()):
                            continue
                        if args.thread and state['thread_id'] != args.thread:
                            continue
                        state = recover(directory)
                        if args.unhandled and state['acknowledgements']:
                            continue
                        tasks.append({k: state[k] for k in ('task_id', 'status', 'exit_code', 'cwd', 'thread_id', 'context', 'finished_at')})
            result = {'tasks': tasks}
        else:
            directory = task_dir(root, args.task_id)
            if args.action == 'wait':
                result = wait_task(directory, args.timeout)
                code = exit_code(result)
            elif args.action == 'cancel':
                result = cancel_task(directory)
            elif args.action == 'ack':
                def acknowledge(state):
                    if state['status'] not in TERMINAL:
                        raise ValueError('cannot acknowledge an unfinished task')
                    state['acknowledgements'].setdefault(args.consumer, now())
                result = change(directory, acknowledge)
            elif args.action == 'notify':
                result = notify_task(directory)
            else:
                result = recover(directory)
            result = summary(result)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return code
    except (ValueError, OSError, RuntimeError) as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=False))
        return 2


if __name__ == '__main__':
    sys.exit(main())
