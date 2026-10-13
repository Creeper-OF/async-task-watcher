#!/usr/bin/env python3
"""Real-process regression tests; run with python3 -m unittest discover -s tests -v."""
import concurrent.futures
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import tempfile
import time
import unittest

CLI = Path(__file__).resolve().parents[1] / 'src/async-task-watcher/scripts/supervisor.py'


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='atw-test-')
        self.cwd = Path(self.tmp.name) / 'work with spaces'
        self.cwd.mkdir()
        self.root = Path(self.tmp.name) / 'state'

    def cli(self, *args, expected=0):
        r = subprocess.run([sys.executable, str(CLI), '--root', str(self.root), *map(str, args)],
                           capture_output=True, text=True, timeout=20)
        self.assertEqual(r.returncode, expected, r.stdout + r.stderr)
        return json.loads(r.stdout)

    def start(self, code, *options):
        return self.cli('start', '--cwd', self.cwd, '--timeout', '12',
                        '--context', 'integration test; verify result then continue', *options,
                        '--', sys.executable, '-c', code)

    def wait(self, task, expected=0):
        return self.cli('wait', task['task_id'], expected=expected)

    def tearDown(self):
        if CLI.exists() and self.root.exists():
            for p in self.root.glob('*/state.json'):
                try:
                    s = json.loads(p.read_text())
                    if s['status'] in ('pending', 'running'):
                        self.cli('cancel', s['task_id'])
                        self.cli('wait', s['task_id'], expected=130)
                except (AssertionError, OSError, ValueError):
                    pass
        self.tmp.cleanup()

    def test_short_command_and_argv_are_not_shell_interpreted(self):
        r = self.cli('run', '--cwd', self.cwd, '--timeout', '5', '--', sys.executable,
                     '-c', 'import os,sys; print(sys.argv[1]); print(os.getcwd())',
                     'a;$(touch NEVER_CREATED) "quote" 中文')
        self.assertEqual(r['status'], 'completed')
        self.assertIn('a;$(touch NEVER_CREATED) "quote" 中文', r['stdout_tail'])
        self.assertIn(str(self.cwd), r['stdout_tail'])
        self.assertFalse((self.cwd / 'NEVER_CREATED').exists())

    def test_long_task_native_event_wait_and_durable_logs(self):
        t = self.start('import time,sys; print("start",flush=True); time.sleep(2); print("end"); print("err",file=sys.stderr)')
        r = self.wait(t)
        self.assertEqual(r['exit_code'], 0)
        self.assertEqual(Path(r['stdout_log']).read_text(), 'start\nend\n')
        self.assertEqual(Path(r['stderr_log']).read_text(), 'err\n')
        self.assertIsNotNone(r['started_at'])
        self.assertIsNotNone(r['finished_at'])
        self.assertIsNotNone(r['pid'])

    def test_early_completion_has_no_sleep_penalty(self):
        t0 = time.monotonic()
        r = self.wait(self.start('print("early")'))
        self.assertEqual(r['status'], 'completed')
        self.assertLess(time.monotonic() - t0, 1.8)

    def test_nonzero_exit_preserves_stderr(self):
        r = self.wait(self.start('import sys; print("bad",file=sys.stderr); sys.exit(7)'), expected=7)
        self.assertEqual(r['status'], 'failed')
        self.assertEqual(r['exit_code'], 7)
        self.assertIn('bad', r['stderr_tail'])

    def test_missing_executable_fails_without_losing_record(self):
        t = self.cli('start', '--cwd', self.cwd, '--timeout', '3', '--', '/nonexistent/atw-command')
        r = self.wait(t, expected=70)
        self.assertEqual(r['status'], 'failed')
        self.assertIn('No such file', r['error'])

    def test_timeout_kills_root_and_child_group(self):
        code = 'import subprocess,sys,time; p=subprocess.Popen([sys.executable,"-c","import time; time.sleep(50)"]); print(p.pid,flush=True); time.sleep(50)'
        t = self.cli('start', '--cwd', self.cwd, '--timeout', '.6', '--', sys.executable, '-c', code)
        r = self.wait(t, expected=124)
        self.assertEqual(r['status'], 'timed_out')
        self.assertTrue(r['timed_out'])
        child = int(Path(r['stdout_log']).read_text().strip())
        self.assertFalse(process_running(child))
        self.assertFalse(process_running(r['pid']))

    def test_timeout_escalates_for_term_ignoring_process(self):
        t = self.cli('start', '--cwd', self.cwd, '--timeout', '.3', '--', sys.executable,
                     '-c', 'import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(50)')
        r = self.wait(t, expected=124)
        self.assertFalse(process_running(r['pid']))

    def test_cancel_is_idempotent_and_keeps_logs(self):
        t = self.start('import time; print("saved",flush=True); time.sleep(50)')
        self.cli('cancel', t['task_id'])
        r = self.wait(t, expected=130)
        self.assertEqual(r['status'], 'cancelled')
        self.assertTrue(r['cancel_requested'])
        self.cli('cancel', t['task_id'])
        self.assertTrue(Path(r['stdout_log']).exists())

    def test_parallel_tasks_do_not_mix_results(self):
        tasks = [self.start(f'import time; time.sleep(.7); print({n!r})') for n in ('one', 'two', 'three')]
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
            results = list(ex.map(self.wait, tasks))
        self.assertEqual(len({r['task_id'] for r in results}), 3)
        self.assertEqual([r['stdout_tail'].strip() for r in results], ['one', 'two', 'three'])

    def test_concurrent_duplicate_key_executes_once(self):
        code = 'from pathlib import Path; import time; p=Path("count"); p.open("a").write("one\\n"); time.sleep(.4)'
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
            tasks = list(ex.map(lambda _: self.start(code, '--key', 'same'), range(3)))
        self.assertEqual(len({r['task_id'] for r in tasks}), 1)
        self.wait(tasks[0])
        self.assertEqual((self.cwd / 'count').read_text(), 'one\n')
        self.cli('start', '--cwd', self.cwd, '--timeout', '12', '--context', 'different',
                 '--key', 'same', '--', sys.executable, '-c', 'print("not same")', expected=2)

    def test_notification_failure_retains_result_and_never_claims_consumed(self):
        notify = json.dumps([sys.executable, '-c', 'import sys; print("notify-fail"); sys.exit(19)'])
        t = self.start('print("result-survives")', '--notify-json', notify)
        r = self.wait(t)
        self.assertEqual(r['status'], 'completed')
        self.assertEqual(r['notification']['status'], 'failed')
        self.assertEqual(r['notification']['exit_code'], 19)
        self.assertIsNone(r['notification']['consumed'])
        self.assertTrue(Path(r['stdout_log']).exists())
        self.assertTrue((self.root / r['task_id'] / 'state.json').exists())

    def test_completed_notifier_cleans_its_finite_process_group(self):
        code = ('import subprocess,sys; '
                'p=subprocess.Popen([sys.executable,"-c","import time; time.sleep(50)"]); '
                'print(p.pid,flush=True)')
        t = self.start('print("job-done")', '--notify-json', json.dumps([sys.executable, '-c', code]))
        r = self.wait(t)
        child = int((self.root / t['task_id'] / 'notification.log').read_text().strip())
        try:
            self.assertFalse(process_running(child), 'notification callback left its child running')
            self.assertEqual(r['notification']['status'], 'delivered')
        finally:
            if process_running(child):
                os.kill(child, signal.SIGKILL)

    def test_notify_and_ack_are_idempotent_without_deletion(self):
        t = self.start('print("done")', '--notify-json', json.dumps([sys.executable, '-c', 'print("delivered")']))
        r = self.wait(t)
        self.assertEqual(r['notification']['status'], 'delivered')
        self.cli('notify', t['task_id'])
        self.cli('notify', t['task_id'])
        self.assertEqual((self.root / t['task_id'] / 'notification.log').read_text(), 'delivered\n')
        a = self.cli('ack', t['task_id'], '--consumer', 'test')
        b = self.cli('ack', t['task_id'], '--consumer', 'test')
        self.assertEqual(a['acknowledgements'], b['acknowledgements'])
        self.assertTrue((self.root / t['task_id'] / 'state.json').exists())

    def test_interrupted_notifier_reported_without_losing_successful_job(self):
        notify = json.dumps([sys.executable, '-c',
                             'import os,signal,time; os.kill(os.getppid(),signal.SIGKILL); time.sleep(50)'])
        t = self.start('print("durable-before-notify")', '--notify-json', notify)
        r = self.wait(t)
        self.assertEqual(r['status'], 'completed')
        self.assertEqual(r['notification']['status'], 'failed')
        self.assertIn('interrupted', r['notification']['error'])
        self.assertEqual(r['notification']['attempts'], 1)
        self.assertEqual(r['stdout_tail'], 'durable-before-notify\n')

    def test_boot_identity_mismatch_is_recovered_as_unknown_not_success(self):
        t = self.start('print("ran-once")')
        r = self.wait(t)
        path = self.root / t['task_id'] / 'state.json'
        state = json.loads(path.read_text())
        state.update(status='running', finished_at=None, exit_code=None)
        # Simulate metadata from a previous OS boot; this is NOT a real host reboot.
        for role in ('supervisor_identity', 'guardian_identity', 'job_identity'):
            state[role]['boot_id'] = 'previous-boot'
            state[role]['pid'] = os.getpid()
        path.write_text(json.dumps(state))
        recovered = self.cli('status', t['task_id'])
        self.assertEqual(recovered['status'], 'failed')
        self.assertIsNone(recovered['exit_code'])
        self.assertIn('unknown', recovered['error'])
        self.assertEqual(Path(r['stdout_log']).read_text(), 'ran-once\n')

    def test_recovery_waits_for_in_flight_start_transaction(self):
        t = self.start('print("valid-result")')
        self.wait(t)
        directory = self.root / t['task_id']
        # A real lock owner stages pending -> completed; status must not mark this
        # transitional record failed while the launch transaction still owns it.
        code = ('import fcntl,json,sys; from pathlib import Path; '
                'root=Path(sys.argv[1]); d=Path(sys.argv[2]); '
                'f=open(root/"start.lock","r+"); fcntl.flock(f,fcntl.LOCK_EX); '
                'p=d/"state.json"; original=p.read_text(); s=json.loads(original); '
                's.update(status="pending",supervisor_identity=None,guardian_identity=None,job_identity=None); '
                'p.write_text(json.dumps(s)); print("locked",flush=True); '
                'sys.stdin.readline(); p.write_text(original)')
        writer = subprocess.Popen([sys.executable, '-c', code, str(self.root), str(directory)],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.assertEqual(writer.stdout.readline().strip(), 'locked')
        waiter = subprocess.Popen([sys.executable, str(CLI), '--root', str(self.root), 'status', t['task_id']],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        fd = os.pidfd_open(waiter.pid)
        try:
            with selectors.DefaultSelector() as sel:
                sel.register(fd, selectors.EVENT_READ)
                self.assertFalse(sel.select(.15), 'recovery raced the active start transaction')
        finally:
            os.close(fd)
            writer.communicate('continue\n', timeout=5)
            out, err = waiter.communicate(timeout=5)
        self.assertEqual(waiter.returncode, 0, err)
        self.assertEqual(json.loads(out)['status'], 'completed')

    def test_waiter_timeout_does_not_cancel_task(self):
        t = self.start('import time; time.sleep(.8); print("kept-running")')
        r = self.cli('wait', t['task_id'], '--timeout', '.05', expected=75)
        self.assertEqual(r['status'], 'running')
        self.assertTrue(r['wait_expired'])
        self.assertEqual(self.wait(t)['status'], 'completed')

    def test_wait_pending_launch_subscribes_after_transaction_commit(self):
        t = self.start('import time; time.sleep(.8); print("committed-running-job")')
        directory = self.root / t['task_id']
        code = ('import fcntl,json,sys; from pathlib import Path; '
                'root=Path(sys.argv[1]); d=Path(sys.argv[2]); '
                'f=open(root/"start.lock","r+"); fcntl.flock(f,fcntl.LOCK_EX); '
                'p=d/"state.json"; original=p.read_text(); s=json.loads(original); '
                's.update(status="pending",supervisor_identity=None,guardian_identity=None); '
                'p.write_text(json.dumps(s)); print("locked",flush=True); '
                'sys.stdin.readline(); p.write_text(original)')
        writer = subprocess.Popen([sys.executable, '-c', code, str(self.root), str(directory)],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.assertEqual(writer.stdout.readline().strip(), 'locked')
        waiter = subprocess.Popen([sys.executable, str(CLI), '--root', str(self.root), 'wait', t['task_id']],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        fd = os.pidfd_open(waiter.pid)
        try:
            with selectors.DefaultSelector() as sel:
                sel.register(fd, selectors.EVENT_READ)
                self.assertFalse(sel.select(.15))
            writer.communicate('commit\n', timeout=5)
            out, err = waiter.communicate(timeout=5)
        finally:
            os.close(fd)
            if writer.poll() is None:
                writer.communicate('commit\n', timeout=5)
            if waiter.poll() is None:
                waiter.communicate(timeout=5)
        self.assertEqual(waiter.returncode, 0, out + err)
        self.assertEqual(json.loads(out)['status'], 'completed')

    def test_supervisor_sigkill_guardian_saves_failure_and_cleans_job(self):
        t = self.start('import time; time.sleep(50)')
        os.kill(t['supervisor_pid'], signal.SIGKILL)
        r = self.wait(t, expected=70)
        self.assertEqual(r['status'], 'failed')
        self.assertIn('supervisor', r['error'])
        self.assertFalse(process_running(t['pid']))

    def test_guardian_sigkill_supervisor_saves_failure_and_cleans_job(self):
        t = self.start('import time; time.sleep(50)')
        os.kill(t['guardian_pid'], signal.SIGKILL)
        r = self.wait(t, expected=70)
        self.assertEqual(r['status'], 'failed')
        self.assertIn('guardian', r['error'])
        self.assertFalse(process_running(t['pid']))

    def test_internal_process_death_also_cleans_live_grandchildren(self):
        for role in ('guardian_pid', 'supervisor_pid'):
            with self.subTest(role=role):
                pipe = self.cwd / (role + '.fifo')
                os.mkfifo(pipe, 0o600)
                fd = os.open(pipe, os.O_RDONLY | os.O_NONBLOCK)
                code = ('import subprocess,sys,time; '
                        'p=subprocess.Popen([sys.executable,"-c","import time; time.sleep(50)"]); '
                        f'f=open({str(pipe)!r},"w"); f.write(str(p.pid)); f.close(); '
                        'time.sleep(50)')
                try:
                    t = self.start(code)
                    with selectors.DefaultSelector() as sel:
                        sel.register(fd, selectors.EVENT_READ)
                        self.assertTrue(sel.select(3), 'command did not start its child')
                    child_pid = int(os.read(fd, 128))
                    os.kill(t[role], signal.SIGKILL)
                    r = self.wait(t, expected=70)
                    self.assertFalse(process_running(child_pid), f'{role} left command descendant running')
                    self.assertTrue(r['cleanup_complete'])
                finally:
                    os.close(fd)

    def test_task_survives_launching_client_exit(self):
        t = self.start('import time; time.sleep(.6); print("independent")')
        # start CLI already exited; a fresh CLI process obtains the same result.
        r = self.cli('wait', t['task_id'])
        self.assertEqual(r['stdout_tail'], 'independent\n')

    def test_private_permissions_and_scope_filter(self):
        t = self.start('print("scope")', '--thread', 'scope-test')
        self.wait(t)
        self.assertEqual(self.root.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.root / t['task_id'] / 'state.json').stat().st_mode & 0o777, 0o600)
        listed = self.cli('list', '--thread', 'different')['tasks']
        self.assertEqual(listed, [])
        self.assertEqual(len(self.cli('list', '--thread', 'scope-test')['tasks']), 1)

    def test_list_does_not_recover_tasks_outside_requested_scope(self):
        t = self.start('print("other-thread")', '--thread', 'other-thread')
        self.wait(t)
        path = self.root / t['task_id'] / 'state.json'
        state = json.loads(path.read_text())
        state.update(status='running', finished_at=None, exit_code=None)
        for role in ('supervisor_identity', 'guardian_identity', 'job_identity'):
            state[role]['boot_id'] = 'old-boot'
        path.write_text(json.dumps(state))
        self.assertEqual(self.cli('list', '--thread', 'requested-thread')['tasks'], [])
        self.assertEqual(json.loads(path.read_text())['status'], 'running')
        self.assertEqual(self.cli('status', t['task_id'])['status'], 'failed')

    def test_traversal_rejected(self):
        self.cli('status', '../elsewhere', expected=2)


def process_running(pid):
    try:
        stat = Path(f'/proc/{pid}/stat').read_text()
        return stat.rsplit(')', 1)[1].split()[0] != 'Z'
    except FileNotFoundError:
        return False


if __name__ == '__main__':
    unittest.main(verbosity=2)
