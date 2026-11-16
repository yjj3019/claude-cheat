import concurrent.futures
import contextlib
import io
import json
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock
import routeDashboard as dash
import openrouterAdvisor as route
import startupDiagnostics

class CollectionTests(unittest.TestCase):
    def test_slow_session_does_not_block_healthy_session_and_last_sample_is_marked(self):
        gate = threading.Event()
        records = [{"instance_id": str(i), "port": 10000+i, "nonce": "fixture", "proxy_pid": i} for i in range(2)]
        def read(record):
            if record['instance_id'] == '0': gate.wait(2)
            return {"instance_id":record['instance_id']}, {"requests":1}
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool, \
             mock.patch.object(dash, '_SESSION_POOL', pool), mock.patch.object(dash, '_SESSION_LAST', {}), \
             mock.patch.object(dash, '_SESSION_PENDING', {}), mock.patch.object(dash, '_session_from_record', side_effect=read):
            try:
                start=time.monotonic(); rows=dash._collect_sessions(records, budget=.05)
                self.assertLess(time.monotonic()-start, .5)
                self.assertEqual([r['session']['instance_id'] for r in rows], ['1'])
                self.assertFalse(rows[0]['metrics']['stale'])
                gate.set(); dash._collect_sessions(records, budget=.1)
                with mock.patch.object(dash, '_session_from_record', return_value=(None,None)):
                    # Drain any outstanding first-round result, then force failed refreshes.
                    dash._collect_sessions(records, budget=.1)
                    rows=dash._collect_sessions(records, budget=.1)
                self.assertTrue(all(r['metrics']['stale'] for r in rows))
                self.assertTrue(all(r['metrics']['collected_at'] for r in rows))
            finally: gate.set()

    def test_local_queue_uses_the_upstream_queue_not_body_admission(self):
        row=dash._session_summary({"session":{"provider":"local"},"metrics":{
            "requestAdmission":{"active":5},"localAdmission":{"active":2,"queued":3,"concurrency":2}}})
        self.assertEqual(row['admission_queued'],3)
        self.assertEqual(row['local_active'],2)

    def test_dual_provider_local_queue_is_independent_of_launch_provider(self):
        row=dash._session_summary({"session":{"provider":"openrouter"},"metrics":{
            "configuration":{"alternate":{"provider":"local","model":"fixture"}},
            "requestAdmission":{"active":5,"queued":99},
            "localAdmission":{"active":2,"queued":3,"concurrency":2}}})
        self.assertEqual(row['admission_queued'],3)
        self.assertEqual(row['local_queued'],3)
        self.assertEqual(row['local_active'],2)

    def test_simultaneous_collectors_share_probes_without_serial_waits(self):
        gate = threading.Event()
        barrier = threading.Barrier(4)
        records = [{"instance_id":str(i),"port":10000+i,"nonce":"fixture","proxy_pid":i} for i in range(2)]
        def read(record):
            if record['instance_id']=='0': gate.wait(3)
            return {"instance_id":record['instance_id']},{"requests":1}
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool, \
             mock.patch.object(dash,'_SESSION_POOL',pool), mock.patch.object(dash,'_SESSION_PENDING',{}), \
             mock.patch.object(dash,'_SESSION_LAST',{}), mock.patch.object(dash,'_session_from_record',side_effect=read) as probe:
            def collect(_):
                barrier.wait(); start=time.monotonic()
                rows=dash._collect_sessions(records,budget=.2)
                return time.monotonic()-start,rows
            try:
                with concurrent.futures.ThreadPoolExecutor(max_workers=4) as callers:
                    results=list(callers.map(collect,range(4)))
                for elapsed,rows in results:
                    self.assertLess(elapsed,.5)
                    self.assertEqual([row['session']['instance_id'] for row in rows],['1'])
                    self.assertFalse(rows[0]['metrics']['stale'])
                self.assertEqual(probe.call_count,2)
            finally: gate.set()

    def test_lock_contention_is_included_in_collection_budget(self):
        lock=threading.Lock(); lock.acquire()
        try:
            with mock.patch.object(dash,'_SESSION_COLLECT_LOCK',lock):
                start=time.monotonic()
                self.assertEqual(dash._collect_sessions([],budget=.05),[])
                self.assertLess(time.monotonic()-start,.3)
        finally: lock.release()


class BrowserCommandTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(mock.patch.object(dash,'_BROWSER_TABS',{}))
        self.enterContext(mock.patch.object(dash,'_BROWSER_ACKS',{}))

    def heartbeat(self, **values):
        return dash.browser_command({'action':'heartbeat','tab':'fixture-tab','visible':True,**values})

    def test_presence_is_not_acknowledgement_and_cancel_prevents_late_delivery(self):
        self.heartbeat()
        command=dash.browser_command({'action':'open','setup':True})
        query={'action':'result','request_id':command['request_id']}
        self.assertEqual(dash.browser_command(query),{'acknowledged':False,'pending':True})
        self.assertFalse(dash.browser_command({**query,'action':'cancel'})['acknowledged'])
        self.assertIsNone(self.heartbeat()['command'])
        self.assertFalse(dash.browser_command(query)['pending'])

    def test_ack_survives_navigation_and_wins_a_cancellation_race(self):
        self.heartbeat()
        command=dash.browser_command({'action':'open','setup':True})
        self.heartbeat(ack=command['request_id'])
        dash.browser_command({'action':'close','tab':'fixture-tab'})
        for action in ('result','cancel'):
            self.assertTrue(dash.browser_command({'action':action,'request_id':command['request_id']})['acknowledged'])

    def test_unresponsive_tab_command_expires_without_late_execution(self):
        with mock.patch.object(dash.time,'monotonic',return_value=0):
            self.heartbeat()
            command=dash.browser_command({'action':'open','setup':True})
        with mock.patch.object(dash.time,'monotonic',return_value=dash.BROWSER_COMMAND_TIMEOUT_SECONDS+1):
            self.assertIsNone(self.heartbeat(ack=command['request_id'])['command'])
            self.assertEqual(dash.browser_command({'action':'result','request_id':command['request_id']}),
                             {'acknowledged':False,'pending':False})

class StartupTests(unittest.TestCase):
    def test_history_contains_only_fixed_diagnostic_fields(self):
        with tempfile.TemporaryDirectory() as root:
            startupDiagnostics.record(root,'fixture','local','bind','port_in_use',1)
            self.assertEqual(startupDiagnostics.recent(root)[0]['code'],'port_in_use')

    @unittest.skipUnless(shutil.which('node') and shutil.which('claude'), 'real node and Claude executable required')
    def test_launcher_owns_a_real_proxy_and_records_readiness(self):
        # Actual Popen, stdout readiness and authenticated loopback ownership check.
        # Only upstream inference and Claude compatibility checks are isolated here.
        root=route.ROOT
        with tempfile.TemporaryDirectory() as directory:
            config={'SONNET_PROVIDER':'local','LOCAL_LLM_URL':'http://127.0.0.1:11434/v1/messages',
                    'LOCAL_LLM_MODEL':'fixture','LOCAL_LLM_API_KEY':'fixture-secret'}
            records=[]
            original=startupDiagnostics.record
            def record(*args):
                value=original(Path(directory),*args[1:]);records.append(value);return value
            with mock.patch.object(route,'check_connection',return_value=0), \
                 mock.patch.object(startupDiagnostics,'record',side_effect=record), \
                 mock.patch.object(route,'write_handoff'), mock.patch.object(route,'clear_handoff'), \
                 mock.patch.object(route,'instance_directory',return_value=Path(directory)), \
                 mock.patch.dict('os.environ', {'ROUTE_PREWARM':'0'}):
                with route._routed_session('local',local_only=True,_config=config) as env:
                    self.assertTrue(env['ANTHROPIC_BASE_URL'].startswith('http://127.0.0.1:'))
            self.assertEqual(records[-1]['code'],'ready')

if __name__ == '__main__': unittest.main()
