import threading
import unittest

from local_agent.approvals import ApprovalBroker, ApprovalError, RunControl, RunStopped


class Clock:
    def __init__(self):
        self.now = 1000.0
        self.lock = threading.Lock()

    def __call__(self):
        with self.lock:
            return self.now

    def advance(self, seconds):
        with self.lock:
            self.now += seconds


class RunControlTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.control = RunControl(threading.Event(), clock=self.clock)

    def test_active_budget_expires_at_boundary(self):
        self.clock.advance(119)
        self.control.check()
        self.assertEqual(self.control.remaining(), 1)
        self.clock.advance(1)
        with self.assertRaises(RunStopped) as caught:
            self.control.check()
        self.assertEqual(caught.exception.code, 'RUN_TIMEOUT')

    def test_waiting_is_excluded_from_active_budget(self):
        self.clock.advance(10)
        with self.control.suspend():
            self.clock.advance(180)
            self.control.check()
            self.assertEqual(self.control.remaining(), 110)
            self.assertEqual(self.control.approval_remaining(), 120)
        self.clock.advance(5)
        self.assertEqual(self.control.remaining(), 105)
        self.assertEqual(self.control.approval_remaining(), 120)

    def test_waiting_budget_is_cumulative(self):
        with self.control.suspend():
            self.clock.advance(200)
        with self.control.suspend():
            self.clock.advance(100)
        self.assertEqual(self.control.approval_remaining(), 0)
        self.assertEqual(self.control.remaining(), 120)

    def test_cancel_prevents_commit(self):
        self.control.cancel_run()
        published = []
        with self.assertRaises(RunStopped) as caught:
            self.control.commit(lambda: published.append('file'))
        self.assertEqual(caught.exception.code, 'CANCELLED')
        self.assertEqual(published, [])

    def test_commit_and_cancel_share_one_boundary(self):
        publishing = threading.Event()
        release = threading.Event()
        cancelled = threading.Event()
        published = []

        def publish():
            publishing.set()
            release.wait(1)
            published.append('file')
            return 'receipt'

        result = []
        worker = threading.Thread(target=lambda: result.append(self.control.commit(publish)))
        worker.start()
        self.assertTrue(publishing.wait(1))
        canceller = threading.Thread(target=lambda: (self.control.cancel_run(), cancelled.set()))
        canceller.start()
        try:
            self.assertFalse(cancelled.wait(.02))
        finally:
            release.set()
            worker.join(1)
            canceller.join(1)
        self.assertEqual(result, ['receipt'])
        self.assertEqual(published, ['file'])
        self.assertTrue(cancelled.is_set())


class ApprovalTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.control = RunControl(threading.Event(), clock=self.clock)
        self.events = []
        self.ready = threading.Event()

        def publish(event, data):
            self.events.append((event, data))
            if event == 'approval.required':
                self.ready.set()

        self.broker = ApprovalBroker('run-1', publish=publish, clock=self.clock)
        self.addCleanup(self.broker.close)

    def start_request(self, arguments=None, preview=None):
        self.ready.clear()
        result = {}

        def request():
            try:
                result['decision'] = self.broker.request(
                    'call-1', 'write_file', arguments or {'path': 'report.md', 'content': 'report'},
                    preview or {'action_summary': '新建 report.md', 'risk': 'medium',
                                'source': 'builtin', 'content': 'report'}, self.control)
            except Exception as error:
                result['error'] = error

        worker = threading.Thread(target=request, daemon=True)
        worker.start()
        self.addCleanup(worker.join, 1)
        self.assertTrue(self.ready.wait(1), result)
        return worker, result, self.broker.snapshot()['id']

    def finish(self, worker, result, decision=None, code=None):
        worker.join(1)
        self.assertFalse(worker.is_alive(), 'Approval wait did not stop')
        if code:
            self.assertEqual(result['error'].code, code)
        else:
            self.assertEqual(result, {'decision': decision})
        self.assertIsNone(self.broker.snapshot())

    def test_allow_and_deny_are_returned(self):
        for decision in ('allow', 'deny'):
            with self.subTest(decision=decision):
                worker, result, approval_id = self.start_request()
                self.assertEqual(self.broker.decide(approval_id, decision), decision)
                self.finish(worker, result, decision=decision)

    def test_identical_decision_is_idempotent_after_wait_finishes(self):
        worker, result, approval_id = self.start_request()
        self.broker.decide(approval_id, 'allow')
        self.finish(worker, result, decision='allow')
        self.assertEqual(self.broker.decide(approval_id, 'allow'), 'allow')
        self.assertEqual([e for e, _ in self.events].count('approval.resolved'), 1)

    def test_conflicting_decision_is_rejected(self):
        worker, result, approval_id = self.start_request()
        self.broker.decide(approval_id, 'deny')
        with self.assertRaises(ApprovalError) as caught:
            self.broker.decide(approval_id, 'allow')
        self.assertEqual(caught.exception.code, 'APPROVAL_CONFLICT')
        self.finish(worker, result, decision='deny')

    def test_wrong_id_and_invalid_decision_do_not_authorize(self):
        worker, result, approval_id = self.start_request()
        for identifier, decision, code in (
                ('other-run-id', 'allow', 'APPROVAL_NOT_FOUND'),
                (approval_id, 'yes', 'APPROVAL_INVALID_DECISION')):
            with self.assertRaises(ApprovalError) as caught:
                self.broker.decide(identifier, decision)
            self.assertEqual(caught.exception.code, code)
        self.assertEqual(self.broker.snapshot()['id'], approval_id)
        self.broker.decide(approval_id, 'deny')
        self.finish(worker, result, decision='deny')

    def test_cancel_wakes_waiter_and_rejects_late_permission(self):
        worker, result, approval_id = self.start_request()
        self.control.cancel_run()
        with self.assertRaises(ApprovalError) as caught:
            self.broker.decide(approval_id, 'allow')
        self.assertEqual(caught.exception.code, 'CANCELLED')
        self.finish(worker, result, code='CANCELLED')
        self.assertIsInstance(result['error'], RunStopped)

    def test_direct_cancel_event_is_observed_without_decision(self):
        worker, result, _ = self.start_request()
        self.control.cancel.set()
        self.finish(worker, result, code='CANCELLED')

    def test_cancelled_run_cannot_retry_a_previous_allow(self):
        worker, result, approval_id = self.start_request()
        self.broker.decide(approval_id, 'allow')
        self.finish(worker, result, decision='allow')
        self.control.cancel_run()
        with self.assertRaises(ApprovalError) as caught:
            self.broker.decide(approval_id, 'allow')
        self.assertEqual(caught.exception.code, 'CANCELLED')

    def test_expiry_rejects_decision_at_exact_deadline(self):
        worker, result, approval_id = self.start_request()
        self.clock.advance(300)
        with self.assertRaises(ApprovalError) as caught:
            self.broker.decide(approval_id, 'allow')
        self.assertEqual(caught.exception.code, 'APPROVAL_EXPIRED')
        self.finish(worker, result, code='APPROVAL_EXPIRED')

    def test_expiry_wakes_without_a_decision(self):
        worker, result, _ = self.start_request()
        self.clock.advance(301)
        self.finish(worker, result, code='APPROVAL_EXPIRED')

    def test_wait_beyond_run_timeout_still_allows_execution(self):
        worker, result, approval_id = self.start_request()
        self.clock.advance(180)
        self.broker.decide(approval_id, 'allow')
        self.finish(worker, result, decision='allow')
        self.assertEqual(self.control.remaining(), 120)
        self.clock.advance(4)
        self.assertEqual(self.control.commit(lambda: 'receipt'), 'receipt')
        self.assertEqual(self.control.remaining(), 116)

    def test_two_approvals_share_cumulative_wait_budget(self):
        worker, result, approval_id = self.start_request()
        self.clock.advance(200)
        self.broker.decide(approval_id, 'allow')
        self.finish(worker, result, decision='allow')
        worker, result, approval_id = self.start_request()
        pending = self.broker.snapshot()
        self.assertEqual(pending['expires_at'] - pending['created_at'], 100)
        self.clock.advance(100)
        with self.assertRaises(ApprovalError) as caught:
            self.broker.decide(approval_id, 'allow')
        self.assertEqual(caught.exception.code, 'APPROVAL_EXPIRED')
        self.finish(worker, result, code='APPROVAL_EXPIRED')

    def test_snapshot_freezes_input_and_returns_independent_copies(self):
        arguments = {'path': 'report.md', 'content': 'original', 'nested': {'v': 1}}
        preview = {'action_summary': '新建', 'risk': 'medium', 'source': 'builtin',
                   'content': 'original', 'nested': ['original']}
        worker, result, approval_id = self.start_request(arguments, preview)
        arguments['nested']['v'] = 2
        arguments['content'] = 'mutated'
        preview['nested'].append('mutated')
        pending = self.broker.snapshot()
        pending['arguments']['content'] = 'snapshot-mutated'
        pending['preview']['nested'].append('snapshot-mutated')
        pending = self.broker.snapshot()
        self.assertEqual(pending['arguments']['content'], 'original')
        self.assertEqual(pending['arguments']['nested'], {'v': 1})
        self.assertEqual(pending['preview']['nested'], ['original'])
        self.assertEqual(pending['content'], 'original')
        self.assertEqual((pending['run_id'], pending['call_id'], pending['name']),
                         ('run-1', 'call-1', 'write_file'))
        self.broker.decide(approval_id, 'allow')
        self.finish(worker, result, decision='allow')
        self.assertNotIn('original', repr(self.events))

    def test_close_invalidates_and_wakes_waiter(self):
        worker, result, approval_id = self.start_request()
        self.broker.close()
        self.finish(worker, result, code='APPROVAL_UNAVAILABLE')
        with self.assertRaises(ApprovalError) as caught:
            self.broker.decide(approval_id, 'allow')
        self.assertEqual(caught.exception.code, 'APPROVAL_UNAVAILABLE')

    def test_callbacks_do_not_hold_broker_or_control_locks(self):
        checked = []

        def publish(event, data):
            if event != 'approval.required':
                return
            reader = threading.Thread(target=lambda: checked.append(
                (self.broker.snapshot()['id'], self.control.remaining())))
            reader.start()
            reader.join(1)
            self.assertFalse(reader.is_alive(), 'Callback held a shared lock')
            self.broker.decide(data['id'], 'allow')

        self.broker = ApprovalBroker('run-1', publish=publish, clock=self.clock)
        self.addCleanup(self.broker.close)
        self.assertEqual(self.broker.request('call-1', 'write_file', {}, {}, self.control), 'allow')
        self.assertEqual(checked[0][1], 120)
