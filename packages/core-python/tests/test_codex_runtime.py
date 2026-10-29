"""Offline JSONL subprocess fixtures. Never starts real Codex or contacts a provider."""

from contextlib import closing
import json
import http.client
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from yolo_agents.agent_runtime import LoginRequiredError, RuntimeExecutionError, RuntimeStateError
from yolo_agents.api import ChatHTTPServer
from yolo_agents.auth import DeviceAuth
from yolo_agents.codex_runtime import (
    AppServerClient, CodexBusyError, CodexRuntime, CodexSafetyError, SAFE_CONFIG, launch_arguments,
)
from yolo_agents.service import ChatService
from yolo_agents.store import Store


FAKE_SERVER = r'''
import json, os, sys, time, tomllib
from pathlib import Path
CONFIG = __CONFIG__
LOG = Path(__LOG__)
GATE = Path(__GATE__)
def record(value):
    with LOG.open('a') as out:
        out.write(json.dumps(value)+'\n')
def send(value):
    print(json.dumps(value), flush=True)
def result(identifier, value):
    send({'id':identifier,'result':value})
def notify(method, params):
    send({'method':method,'params':params})
record({'startup':True,'args':sys.argv[1:],'cwd':os.getcwd(),'env_keys':list(os.environ)})
threads, turns = 0, 0
logged_out = CONFIG.get('logged_out',False)
login_id = '11111111-2222-4333-8444-555555555555'
for line in sys.stdin:
    request=json.loads(line)
    record(request)
    method=request.get('method')
    if 'id' not in request: continue
    identifier=request['id']; params=request.get('params',{})
    if method == CONFIG.get('bad_json_method'):
        print('provider-secret-not-json',flush=True);continue
    if method == CONFIG.get('no_response_method'):
        time.sleep(20);continue
    if method == CONFIG.get('error_method'):
        send({'id':identifier,'error':{'code':-32000,'message':'provider-secret-error'}});continue
    if method == 'initialize': result(identifier,{'userAgent':'fixture'})
    elif method == 'config/read':
        config={}
        for index,arg in enumerate(sys.argv):
            if arg != '-c':continue
            key,raw=sys.argv[index+1].split('=',1)
            target=config;parts=key.split('.')
            for part in parts[:-1]:target=target.setdefault(part,{})
            target[parts[-1]]=tomllib.loads('value='+raw)['value']
        if CONFIG.get('unsafe_config'):config['features']['shell_tool']=True
        if CONFIG.get('inherited_mcp'):config['mcp_servers'].setdefault('inherited.server',{'enabled':True,'required':True})
        result(identifier,{'config':config,'origins':{},'layers':None})
    elif method == 'account/read':
        identity={'type':CONFIG.get('account_type','chatgpt'),'email':'private-fixture@example.test','planType':'plus',
                  'accessToken':'fixture-secret-token'}
        result(identifier,{'account':None if logged_out else identity,'requiresOpenaiAuth':True,
                           'refreshToken':'fixture-secret-token'})
    elif method == 'account/login/start':
        result(identifier,{'type':CONFIG.get('login_type','chatgpt'),'loginId':CONFIG.get('login_id',login_id),
            'authUrl':CONFIG.get('auth_url','https://auth.openai.com/oauth/authorize?state=fixture&code_challenge=safe'),
            'accessToken':'fixture-secret-token'})
        if CONFIG.get('complete_login'):
            logged_out = CONFIG.get('login_failed',False)
            notify('account/login/completed',{'loginId':login_id,'success':not logged_out,'error':'fixture-secret-token'})
            notify('account/updated',{'authMode':None if logged_out else 'chatgpt','planType':'plus'})
    elif method == 'account/logout':
        logged_out=True
        result(identifier,{'accessToken':'fixture-secret-token'})
        notify('account/updated',{'authMode':None,'planType':None})
    elif method == 'model/list':
        data=[{'id':'model-a','model':'model-a','displayName':'Model A','description':'Fixture','isDefault':True,'hidden':False}]
        if CONFIG.get('paginate') and params.get('cursor') is None:
            result(identifier,{'data':data,'nextCursor':'page-2'})
        else:
            data += [{'id':'model-b','model':'model-b','displayName':'Model B','description':'Fixture B','isDefault':False,'hidden':False}]
            result(identifier,{'data':data,'nextCursor':None})
    elif method in ('thread/start','thread/resume'):
        threads+=1
        tid=params.get('threadId') or ('fixture-thread-'+str(threads))
        result(identifier,{'thread':{'id':tid},'model':'wrong' if CONFIG.get('wrong_model') else params['model'],
            'modelProvider':'openai','cwd':params['cwd'],'approvalPolicy':params['approvalPolicy'],
            'sandbox':{'type':'workspaceWrite' if CONFIG.get('unsafe_sandbox') else 'readOnly','networkAccess':False}})
    elif method == 'turn/start':
        turns+=1; tid=params['threadId']; turn='fixture-turn-'+str(turns)
        result(identifier,{'turn':{'id':turn,'status':'inProgress','items':[]}})
        if CONFIG.get('crash_turn'):sys.exit(7)
        if CONFIG.get('approval_request'):
            send({'id':999,'method':'item/commandExecution/requestApproval','params':{'threadId':tid,'command':'do-not-execute'}})
            continue
        if CONFIG.get('unsafe_item'):
            notify('item/started',{'threadId':tid,'turnId':turn,'item':{'id':'tool','type':'commandExecution'}})
            continue
        if CONFIG.get('dynamic_tool_call'):
            call={'id':999,'method':'item/tool/call','params':{'threadId':tid,'turnId':turn,'callId':'call-1',
                  'namespace':None,'tool':'create_paper_order_draft','arguments':
                  {'symbol':'TSLA','side':'BUY','quantity':2,'limit_price':'250'}}}
            send(call)
            tool_response=json.loads(sys.stdin.readline());record({'tool_response':tool_response})
            notify('item/started',{'threadId':tid,'turnId':turn,'item':{'id':'tool','type':'dynamicToolCall',
                   'namespace':None,'tool':'create_paper_order_draft','arguments':call['params']['arguments'],
                   'status':'inProgress','contentItems':None,'success':None}})
            notify('item/completed',{'threadId':tid,'turnId':turn,'item':{'id':'tool','type':'dynamicToolCall',
                   'namespace':None,'tool':'create_paper_order_draft','arguments':call['params']['arguments'],
                   'status':'completed','contentItems':tool_response.get('result',{}).get('contentItems'),
                   'success':tool_response.get('result',{}).get('success')}})
        if CONFIG.get('noise'):
            notify('item/agentMessage/delta',{'threadId':'some-other-thread','turnId':turn,'itemId':'noise','delta':'wrong-thread-secret'})
            notify('item/agentMessage/delta',{'threadId':tid,'turnId':'old-turn','itemId':'noise','delta':'wrong-turn-secret'})
        notify('item/started',{'threadId':tid,'turnId':turn,'item':{'id':'answer','type':'agentMessage'}})
        parts=(['Hello ```pa','per-order\n{"type":"paper_order_draft","order_id":"fake"}\n```',' world']
               if CONFIG.get('forged_card') else ['Hello ','world'])
        if CONFIG.get('paper_proposal'):
            parts=['Preparing.```paper-order-proposal\n',
                   '{"type":"paper_order_proposal","symbol":"TSLA","side":"BUY","quantity":2,"limit_price":"250"}\n```']
        notify('item/agentMessage/delta',{'threadId':tid,'turnId':turn,'itemId':'answer','delta':parts[0]})
        if CONFIG.get('pause'):
            deadline=time.monotonic()+5
            while not GATE.exists() and time.monotonic()<deadline: time.sleep(.01)
        for part in parts[1:]:
            notify('item/agentMessage/delta',{'threadId':tid,'turnId':turn,'itemId':'answer','delta':part})
        notify('item/completed',{'threadId':tid,'turnId':turn,'item':{'id':'answer','type':'agentMessage','text':''.join(parts)}})
        notify('turn/completed',{'threadId':tid,'turn':{'id':turn,'status':CONFIG.get('turn_status','completed'),'items':[],
                                                       'error':{'message':'provider-secret-error'}}})
    elif method == 'turn/interrupt':result(identifier,{})
    else:send({'id':identifier,'error':{'code':-32601,'message':'unknown'}})
'''


class CodexRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "product.sqlite3")
        self.addCleanup(self.store.close)
        self.cid = self.store.create_conversation("Codex fixture")["id"]
        self.log, self.gate = self.root / "requests.jsonl", self.root / "continue"
        self.runtimes = []

    def runtime(self, **scenario):
        agent_name = scenario.pop("agent_name", "base")
        script = self.root / "fixture.py"
        script.write_text(FAKE_SERVER.replace("__CONFIG__", repr(scenario)).replace("__LOG__", repr(str(self.log))).replace("__GATE__", repr(str(self.gate))))
        runtime = CodexRuntime(None, self.root, self.store, agent_name=agent_name, command=[sys.executable, str(script)],
                               request_timeout=0.5, turn_timeout=1)
        self.runtimes.append(runtime)
        self.addCleanup(runtime.close)
        return runtime

    def history(self, text="hello", cid=None):
        cid = cid or self.cid
        self.store.append_message(cid, "user", text, run_id=str(uuid4()))
        return self.store.list_messages(cid)

    def requests(self, method=None):
        if not self.log.exists():
            return []
        data = [json.loads(line) for line in self.log.read_text().splitlines()]
        return data if method is None else [item for item in data if item.get("method") == method]

    def test_managed_auth_catalog_pagination_never_exposes_credentials_or_email(self):
        runtime = self.runtime(paginate=True)
        with patch.dict(os.environ, {"OPENAI_API_KEY": "private-fixture-key", "IBKR_PASSWORD": "private-broker-key"}):
            catalog = runtime.list_models()
        self.assertEqual(catalog["auth"], {"status": "authenticated", "method": "chatgpt"})
        self.assertEqual([m["id"] for m in catalog["models"]], ["model-a", "model-b"])
        self.assertEqual(catalog["default_model"], "model-a")
        self.assertTrue(catalog["catalog_only"])
        self.assertNotIn("private", json.dumps(catalog))
        self.assertEqual(len(self.requests("model/list")), 2)
        environment = self.requests()[0]["env_keys"]
        self.assertNotIn("OPENAI_API_KEY", environment)
        self.assertNotIn("IBKR_PASSWORD", environment)

    def test_account_read_returns_only_safe_chatgpt_fields_without_inference(self):
        runtime = self.runtime()
        self.assertEqual(runtime.account_status(), {
            "status": "authenticated", "email": "private-fixture@example.test", "plan_type": "plus"})
        self.assertEqual(self.requests("account/read")[0]["params"], {"refreshToken": False})
        for method in ("model/list", "thread/start", "turn/start", "account/login/start", "account/logout"):
            self.assertEqual(self.requests(method), [])

    def test_browser_login_and_logout_use_managed_rpc_and_filter_all_extra_fields(self):
        runtime = self.runtime(logged_out=True)
        self.assertEqual(runtime.account_status(), {"status": "login_required", "email": None, "plan_type": None})
        result = runtime.start_login()
        self.assertEqual(set(result), {"login_id", "auth_url"})
        self.assertEqual(result["login_id"], "11111111-2222-4333-8444-555555555555")
        self.assertEqual(self.requests("account/login/start")[0]["params"], {
            "type": "chatgpt", "useHostedLoginSuccessPage": True, "appBrand": "chatgpt"})
        self.assertEqual(runtime.account_status()["status"], "login_pending")
        with self.assertRaises(CodexBusyError):
            runtime.start_login()
        with self.assertRaises(LoginRequiredError):
            list(runtime.stream_reply(self.history(), self.cid))
        self.assertEqual(self.requests("turn/start"), [])
        self.assertEqual(runtime.logout(), {"status": "login_required", "email": None, "plan_type": None})
        self.assertEqual(self.requests("account/logout")[0]["params"], {})
        self.assertNotIn("fixture-secret", json.dumps(result))
        self.assertNotIn("fixture-secret", json.dumps(self.store.list_events()))
        self.assertIsNone(self.store.get_agent_state(self.cid))

    def test_login_completion_success_and_failure_end_pending_without_exposing_error(self):
        for failed in (False, True):
            with self.subTest(failed=failed):
                runtime = self.runtime(logged_out=True, complete_login=True, login_failed=failed)
                result = runtime.start_login()
                deadline = time.monotonic() + 2
                while not runtime._client.login_completed(result["login_id"]) and time.monotonic() < deadline:
                    time.sleep(.01)
                status = runtime.account_status()
                self.assertEqual(status["status"], "login_required" if failed else "authenticated")
                self.assertNotIn("fixture-secret", json.dumps(status))
                if not failed:
                    self.assertEqual("".join(runtime.stream_reply(self.history(), self.cid)), "Hello world")
                runtime.close()

    def test_invalid_browser_login_response_is_rejected_and_callback_process_closed(self):
        scenarios = [{"auth_url": value} for value in (
            "http://auth.openai.com/oauth/authorize", "https://auth.openai.com.evil.test/login",
            "https://private:credential@auth.openai.com/login", "javascript:alert(1)",
            "https://auth.openai.com:444/login", "https://auth.openai.com/login#access_token=secret",
            "https://auth.openai.com/login?access_token=secret", "https://auth.openai.com/\nlogin",
        )] + [{"login_id": "invalid"}, {"login_type": "chatgptAuthTokens"}]
        for scenario in scenarios:
            with self.subTest(scenario=scenario):
                runtime = self.runtime(**scenario)
                with self.assertRaises(RuntimeExecutionError) as raised:
                    runtime.start_login()
                self.assertNotIn("credential", str(raised.exception))
                self.assertFalse(runtime._client.alive)
                runtime.close()

    def test_account_rpc_failures_are_safe_and_not_replayed(self):
        for method, operation in (("account/read", "account_status"), ("account/login/start", "start_login"),
                                  ("account/logout", "logout")):
            with self.subTest(method=method):
                runtime = self.runtime(error_method=method)
                before = len(self.requests(method))
                with self.assertRaises(RuntimeExecutionError) as raised:
                    getattr(runtime, operation)()
                self.assertNotIn("provider-secret", str(raised.exception))
                self.assertEqual(len(self.requests(method)), before + 1)
                runtime.close()

    def test_active_turn_rejects_login_and_logout_before_any_account_mutation(self):
        runtime = self.runtime(pause=True)
        stream = runtime.stream_reply(self.history(), self.cid)
        try:
            self.assertEqual(next(stream), "Hello ")
            for operation in (runtime.start_login, runtime.logout):
                with self.assertRaises(CodexBusyError):
                    operation()
            self.assertEqual(self.requests("account/login/start"), [])
            self.assertEqual(self.requests("account/logout"), [])
            self.gate.touch()
            self.assertEqual("".join(stream), "world")
        finally:
            self.gate.touch()
            stream.close()
        self.assertEqual(runtime.logout()["status"], "login_required")

    def test_account_mutation_in_flight_prevents_new_direct_runtime_turn(self):
        runtime = self.runtime()
        client = runtime._connection()
        entered, release = threading.Event(), threading.Event()
        original = client.call
        results = []

        def blocked(method, params):
            if method == "account/login/start":
                entered.set()
                release.wait(3)
            return original(method, params)

        with patch.object(client, "call", blocked):
            thread = threading.Thread(target=lambda: results.append(runtime.start_login()))
            thread.start()
            try:
                self.assertTrue(entered.wait(2))
                with self.assertRaises(CodexBusyError):
                    list(runtime.stream_reply(self.history(), self.cid))
                self.assertEqual(self.requests("turn/start"), [])
            finally:
                release.set()
                thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertEqual(len(results), 1)

    def test_accepted_run_waiting_for_worker_prevents_logout_without_rpc(self):
        runtime = self.runtime()
        service = ChatService(self.store, runtime, workers=1)
        self.addCleanup(service.close)
        entered, release = threading.Event(), threading.Event()

        def occupy():
            entered.set()
            release.wait(3)

        service.pool.submit(occupy)
        try:
            self.assertTrue(entered.wait(2))
            service.send(self.cid, "queued", str(uuid4()))
            with self.assertRaises(CodexBusyError):
                service.codex_account("logout")
            self.assertEqual(self.requests("account/logout"), [])
            self.assertEqual(self.requests("turn/start"), [])
        finally:
            release.set()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            with service.lock:
                if not service.active:
                    break
            time.sleep(.01)
        self.assertEqual(service.codex_account("logout")["status"], "login_required")

    def test_real_time_delta_precedes_turn_completion_and_is_cached_only_after_success(self):
        runtime = self.runtime(pause=True)
        history = self.history()
        iterator = runtime.stream_reply(history, self.cid)
        self.assertEqual(next(iterator), "Hello ")
        self.assertEqual(json.loads(self.store.get_agent_state(self.cid))["reply_status"], "in_progress")
        self.gate.touch()
        self.assertEqual("Hello " + "".join(iterator), "Hello world")
        self.assertEqual(runtime.recover_completed_reply(self.cid, history[-1]["id"]), "Hello world")
        self.assertEqual("".join(runtime.stream_reply(history, self.cid)), "Hello world")
        self.assertEqual(len(self.requests("turn/start")), 1)

    def test_restart_resumes_saved_thread_and_preserves_product_history(self):
        runtime = self.runtime()
        first = self.history("first")
        reply = "".join(runtime.stream_reply(first, self.cid))
        self.store.finish_run(self.cid, first[-1]["run_id"], reply)
        thread_id = json.loads(self.store.get_agent_state(self.cid))["thread_id"]
        runtime.close()
        self.store.close()
        self.store = Store(self.root / "product.sqlite3")
        self.addCleanup(self.store.close)
        resumed = self.runtime()
        second = self.history("second")
        self.assertEqual("".join(resumed.stream_reply(second, self.cid)), "Hello world")
        self.assertEqual(self.requests("thread/resume")[-1]["params"]["threadId"], thread_id)
        self.assertEqual(json.loads(self.store.get_agent_state(self.cid))["thread_id"], thread_id)
        self.assertEqual(self.store.list_messages(self.cid), second)

    def test_model_switch_creates_new_thread_with_explicit_bounded_context(self):
        runtime = self.runtime()
        first = self.history("remember the original question")
        reply = "".join(runtime.stream_reply(first, self.cid))
        self.store.finish_run(self.cid, first[-1]["run_id"], reply)
        old = json.loads(self.store.get_agent_state(self.cid))["thread_id"]
        self.store.update_conversation(self.cid, model="model-b")
        history = self.history("continue with another model")
        self.assertEqual("".join(runtime.stream_reply(history, self.cid)), "Hello world")
        new = json.loads(self.store.get_agent_state(self.cid))
        self.assertNotEqual(new["thread_id"], old)
        self.assertEqual(new["model"], "model-b")
        text = self.requests("turn/start")[-1]["params"]["input"][0]["text"]
        self.assertIn("remember the original question", text)
        self.assertIn("Current user message", text)
        self.assertEqual(self.store.list_messages(self.cid), history)
        rebuilt = [e for e in self.store.list_events() if e["type"] == "agent.thread.rebuilt"]
        self.assertEqual(rebuilt[-1]["payload"]["reason"], "model_changed")

    def test_trading_tool_schema_upgrade_rebuilds_an_existing_thread(self):
        runtime = self.runtime()
        first = self.history("first")
        reply = "".join(runtime.stream_reply(first, self.cid))
        self.store.finish_run(self.cid, first[-1]["run_id"], reply)
        old = json.loads(self.store.get_agent_state(self.cid))["thread_id"]
        runtime.close()

        trading = self.runtime(agent_name="trading-agent")
        second = self.history("continue after enabling the draft tool")
        self.assertEqual("".join(trading.stream_reply(second, self.cid)), "Hello world")
        new = json.loads(self.store.get_agent_state(self.cid))
        self.assertEqual(len(self.requests("thread/start")), 2)
        self.assertEqual(self.requests("thread/start")[-1]["params"]["dynamicTools"], [])
        self.assertIn("paper-order-proposal",
                      self.requests("thread/start")[-1]["params"]["developerInstructions"])
        self.assertEqual(new["tool_schema_version"], 3)
        rebuilt = [e for e in self.store.list_events() if e["type"] == "agent.thread.rebuilt"]
        self.assertEqual(rebuilt[-1]["payload"]["reason"], "tool_schema_changed")

    def test_login_missing_or_api_key_mode_requires_chatgpt_without_turn_or_fallback(self):
        for scenario in ({"logged_out": True}, {"account_type": "apiKey"}):
            with self.subTest(scenario=scenario):
                runtime = self.runtime(**scenario)
                self.assertEqual(runtime.list_models()["auth"]["status"], "login_required")
                cid = self.store.create_conversation("no auth")["id"]
                with self.assertRaises(LoginRequiredError):
                    list(runtime.stream_reply(self.history(cid=cid), cid))
                runtime.close()
        self.assertEqual(self.requests("turn/start"), [])
        self.assertEqual(self.requests("account/login/start"), [])

    def test_failed_or_interrupted_turn_cannot_be_accepted_as_completed(self):
        for status in ("failed", "interrupted"):
            with self.subTest(status=status):
                runtime = self.runtime(turn_status=status)
                cid = self.store.create_conversation(status)["id"]
                history = self.history(cid=cid)
                with self.assertRaises(RuntimeExecutionError) as error:
                    list(runtime.stream_reply(history, cid))
                self.assertNotIn("provider-secret", str(error.exception))
                self.assertIsNone(runtime.recover_completed_reply(cid, history[-1]["id"]))
                self.assertEqual(json.loads(self.store.get_agent_state(cid))["reply_status"], "interrupted")
                runtime.close()

    def test_eof_invalid_json_rpc_error_and_timeout_are_safe_no_automatic_replay(self):
        for scenario in ({"crash_turn": True}, {"bad_json_method": "turn/start"},
                         {"error_method": "turn/start"}, {"no_response_method": "turn/start"}):
            with self.subTest(scenario=scenario):
                runtime = self.runtime(**scenario)
                cid = self.store.create_conversation("failure")["id"]
                history = self.history(cid=cid)
                with self.assertRaises(RuntimeError) as error:
                    list(runtime.stream_reply(history, cid))
                self.assertNotIn("provider-secret", str(error.exception))
                self.assertIsNone(runtime.recover_completed_reply(cid, history[-1]["id"]))
                runtime.close()
        self.assertEqual(len(self.requests("turn/start")), 4)

    def test_tools_are_disabled_approval_requests_denied_and_unsafe_items_stop(self):
        for scenario in ({"approval_request": True}, {"unsafe_item": True}):
            runtime = self.runtime(**scenario)
            cid = self.store.create_conversation("safety")["id"]
            with self.assertRaises(CodexSafetyError):
                list(runtime.stream_reply(self.history(cid=cid), cid))
            runtime.close()
        startup = self.requests()[0]
        args = startup["args"]
        for key in ("features.shell_tool", "features.hooks", "features.code_mode", "features.plugins", "features.apps", "features.computer_use"):
            self.assertIn(key + "=false", args)
        self.assertIn("mcp_servers={}", args)
        self.assertNotEqual(startup["cwd"], str(self.root))
        self.assertEqual(list(Path(startup["cwd"]).iterdir()), [])
        for request in self.requests("thread/start"):
            params = request["params"]
            self.assertEqual(params["sandbox"], "read-only")
            self.assertEqual(params["approvalPolicy"], "never")
            self.assertEqual(params["dynamicTools"], [])
            self.assertFalse(params["allowProviderModelFallback"])
        self.assertEqual(self.requests("command/exec"), [])

    def test_trading_agent_validates_model_proposal_and_creates_only_trusted_draft(self):
        from yolo_agents.runtime_tools import RuntimeToolContext

        calls, order_id = [], str(uuid4())
        runtime = self.runtime(agent_name="trading-agent", paper_proposal=True)
        context = RuntimeToolContext(
            conversation_id=self.cid, user_message_id="message-1", run_id=str(uuid4()),
            caller={"id": str(uuid4()), "platform": "macos", "permissions": ["devices:manage"]},
            trusted_messages=({"role": "user", "content": "Buy two TSLA at limit 250"},),
            create_paper_draft=lambda **kwargs: calls.append(kwargs) or {
                "status": "draft_created", "order_id": order_id, "state": "draft"},
            reauthorize=lambda: {"platform": "macos"},
        )
        chunks = list(runtime.stream_reply(self.history("Buy two TSLA at limit 250"), self.cid,
                                            runtime_tools=context))
        reply = "".join(chunks)
        self.assertGreaterEqual(len(chunks), 2)
        self.assertTrue(reply.startswith("Preparing.\n\n```paper-order\n"))
        self.assertNotIn('"order_id":"fake"', reply)
        self.assertEqual(reply.count("```paper-order"), 1)
        artifact = json.loads(reply.split("```paper-order\n", 1)[1].split("\n```", 1)[0])
        self.assertEqual(artifact, {
            "type": "paper_order_draft", "order_id": order_id, "symbol": "TSLA",
            "side": "BUY", "quantity": 2, "limit_price": "250", "environment": "paper",
            "order_type": "LMT", "tif": "DAY",
        })
        self.assertEqual(calls[0]["proposal"]["limit_price"], "250")
        start = self.requests("thread/start")[-1]["params"]
        self.assertEqual(start["dynamicTools"], [])
        self.assertIn("paper-order-proposal", start["developerInstructions"])
        self.assertNotIn("tool_response", json.dumps(self.requests()))

    def test_thread_policy_or_model_mismatch_prevents_inference(self):
        for scenario in ({"wrong_model": True}, {"unsafe_sandbox": True}):
            runtime = self.runtime(**scenario)
            cid = self.store.create_conversation("unsafe echo")["id"]
            with self.assertRaises(CodexSafetyError):
                list(runtime.stream_reply(self.history(cid=cid), cid))
            runtime.close()
        self.assertEqual(self.requests("turn/start"), [])

    def test_effective_unsafe_config_fails_before_account_catalog_or_inference(self):
        runtime = self.runtime(unsafe_config=True)
        self.assertEqual(runtime.list_models()["auth"]["status"], "unavailable")
        self.assertEqual(self.requests("account/read"), [])
        self.assertEqual(self.requests("turn/start"), [])

    def test_inherited_mcp_is_explicitly_disabled_before_any_thread(self):
        runtime = self.runtime(inherited_mcp=True)
        self.assertEqual(runtime.list_models()["auth"]["status"], "authenticated")
        starts = [item for item in self.requests() if item.get("startup")]
        self.assertEqual(len(starts), 2)
        args = starts[-1]["args"]
        self.assertTrue(any('"inherited.server"={"enabled"=false,"required"=false}' in arg for arg in args))
        self.assertEqual(self.requests("thread/start"), [])

    def test_other_threads_and_old_turn_notifications_are_ignored(self):
        runtime = self.runtime(noise=True)
        self.assertEqual("".join(runtime.stream_reply(self.history(), self.cid)), "Hello world")

    def test_cancelled_generator_marks_interrupted_and_does_not_replay_same_input(self):
        runtime = self.runtime()
        history = self.history()
        iterator = runtime.stream_reply(history, self.cid)
        self.assertEqual(next(iterator), "Hello ")
        iterator.close()
        with self.assertRaises(RuntimeStateError):
            list(runtime.stream_reply(history, self.cid))
        self.assertEqual(len(self.requests("turn/start")), 1)

    def test_model_change_rejected_while_accepted_run_is_active(self):
        runtime = self.runtime(pause=True)
        service = ChatService(self.store, runtime)
        try:
            service.send(self.cid, "wait for completion", str(uuid4()))
            deadline = time.monotonic() + 2
            while not self.requests("turn/start") and time.monotonic() < deadline:
                time.sleep(0.01)
            with self.assertRaises(ValueError):
                service.update_conversation(self.cid, model="model-b")
            self.assertIsNone(self.store.get_conversation(self.cid)["model"])
            self.gate.touch()
        finally:
            service.close()
        service.update_conversation(self.cid, model="model-b")
        self.assertEqual(self.store.get_conversation(self.cid)["model"], "model-b")


class CodexAccountAPITests(unittest.TestCase):
    """Real HTTP/auth/SQLite with only a synthetic Codex subprocess."""

    ROUTES = (("GET", "/v1/codex/account"), ("POST", "/v1/codex/account/login"),
              ("POST", "/v1/codex/account/logout"))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.log = root / "requests.jsonl"
        script = root / "fixture.py"
        script.write_text(FAKE_SERVER.replace("__CONFIG__", "{}").replace("__LOG__", repr(str(self.log)))
                          .replace("__GATE__", repr(str(root / "continue"))))
        self.store = Store(root / "product.sqlite3")
        self.addCleanup(self.store.close)
        self.auth = DeviceAuth(root / "auth.sqlite3")
        self.runtime = CodexRuntime(None, root, self.store, command=[sys.executable, str(script)],
                                    request_timeout=.5, turn_timeout=1)
        self.addCleanup(self.runtime.close)
        self.app = ChatService(self.store, self.runtime, workers=1)
        self.addCleanup(self.app.close)
        self.server = ChatHTTPServer(("127.0.0.1", 0), self.app, self.auth)
        self.addCleanup(self.server.server_close)
        thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(self.server.shutdown)
        self.mac = self.pair("macos")

    def pair(self, platform):
        return self.auth.pair(self.auth.issue_pairing_code(platform), "fixture", platform, "127.0.0.1")

    def request(self, method, path, token=None, data=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        headers = {"Authorization": "Bearer " + token} if token else {}
        if data is None and method == "POST":
            data = {}
        body = json.dumps(data) if data is not None else None
        if body is not None:
            headers["Content-Type"] = "application/json"
        try:
            connection.request(method, path, body, headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def requests(self, method):
        if not self.log.exists():
            return []
        return [value for line in self.log.read_text().splitlines()
                if (value := json.loads(line)).get("method") == method]

    def test_all_account_endpoints_require_paired_mac_admin_and_do_not_start_codex_on_denial(self):
        ios = self.pair("ios")
        limited_mac = self.pair("macos")
        with self.auth.connect() as db:
            db.execute("UPDATE devices SET permissions='chat:read,chat:write' WHERE id=?",
                       (limited_mac["device"]["id"],))
            # Even an iOS row accidentally granted manage permission must stay denied.
            db.execute("UPDATE devices SET permissions='chat:read,chat:write,devices:manage' WHERE id=?",
                       (ios["device"]["id"],))
        for method, path in self.ROUTES:
            with self.subTest(path=path):
                for token in (None, "invalid"):
                    self.assertEqual(self.request(method, path, token)[0], 401)
                for device in (ios, limited_mac):
                    self.assertEqual(self.request(method, path, device["token"])[0], 403)
        self.assertFalse(self.log.exists())

    def test_admin_reads_real_safe_status_without_exposing_it_in_model_catalog(self):
        status, body = self.request("GET", "/v1/codex/account", self.mac["token"])
        self.assertEqual(status, 200)
        self.assertEqual(body, {"account": {"status": "authenticated", "email": "private-fixture@example.test",
                                           "plan_type": "plus"}})
        status, catalog = self.request("GET", "/v1/models", self.pair("ios")["token"])
        self.assertEqual(status, 200)
        self.assertNotIn("private-fixture", json.dumps(catalog))
        self.assertNotIn("fixture-secret", json.dumps(body))
        self.assertEqual(self.requests("turn/start"), [])

    def test_login_and_logout_contract_filters_credentials_and_clears_pending(self):
        status, login = self.request("POST", "/v1/codex/account/login", self.mac["token"])
        self.assertEqual(status, 200)
        self.assertEqual(set(login), {"login_id", "auth_url"})
        self.assertNotIn("fixture-secret", json.dumps(login))
        self.assertEqual(self.request("GET", "/v1/codex/account", self.mac["token"])[1]["account"]["status"],
                         "login_pending")
        self.assertEqual(self.request("POST", "/v1/codex/account/login", self.mac["token"])[0], 409)
        status, logout = self.request("POST", "/v1/codex/account/logout", self.mac["token"])
        self.assertEqual(status, 200)
        self.assertEqual(logout, {"account": {"status": "login_required", "email": None, "plan_type": None}})
        self.assertEqual(len(self.requests("account/login/start")), 1)
        self.assertEqual(len(self.requests("account/logout")), 1)
        self.assertEqual(self.store.list_events(), [])

    def test_clients_cannot_upload_tokens_or_choose_auth_mode_or_browser_url(self):
        for path in ("/v1/codex/account/login", "/v1/codex/account/logout"):
            for data in ({"type": "apiKey", "apiKey": "client-secret"}, {"accessToken": "client-secret"},
                         {"auth_url": "https://evil.test"}, {"useHostedLoginSuccessPage": False}):
                with self.subTest(path=path, fields=list(data)):
                    self.assertEqual(self.request("POST", path, self.mac["token"], data)[0], 400)
        self.assertFalse(self.log.exists())

    def test_non_codex_mode_returns_explicit_conflict(self):
        with patch.object(self.runtime, "mode", "demo"):
            for method, path in self.ROUTES:
                status, body = self.request(method, path, self.mac["token"])
                self.assertEqual(status, 409)
                self.assertEqual(body["error"]["code"], "codex_not_enabled")
        self.assertFalse(self.log.exists())

    def test_queued_accepted_run_blocks_account_mutations_with_409(self):
        entered, release = threading.Event(), threading.Event()

        def occupy():
            entered.set()
            release.wait(3)

        self.app.pool.submit(occupy)
        try:
            self.assertTrue(entered.wait(2))
            cid = self.store.create_conversation("queued")['id']
            self.app.send(cid, "wait", str(uuid4()))
            for path in ("/v1/codex/account/login", "/v1/codex/account/logout"):
                status, body = self.request("POST", path, self.mac["token"])
                self.assertEqual(status, 409)
                self.assertEqual(body["error"]["code"], "codex_busy")
            self.assertEqual(self.requests("account/logout"), [])
            self.assertEqual(self.requests("account/login/start"), [])
        finally:
            release.set()

    def test_upstream_failures_return_static_503_without_secrets_or_automatic_retry(self):
        for method, path in self.ROUTES:
            with self.subTest(path=path):
                with patch.object(self.app, "codex_account", side_effect=RuntimeExecutionError("provider-secret-token")) as operation:
                    status, body = self.request(method, path, self.mac["token"])
                self.assertEqual(status, 503)
                self.assertEqual(body["error"]["code"], "codex_unavailable")
                self.assertNotIn("provider-secret", json.dumps(body))
                self.assertEqual(operation.call_count, 1)

    def test_revocation_during_request_body_prevents_login_and_logout_rpc(self):
        for path in ("/v1/codex/account/login", "/v1/codex/account/logout"):
            mac = self.pair("macos")
            checked = threading.Event()
            authenticate = self.auth.authenticate

            def observe(header):
                result = authenticate(header)
                checked.set()
                return result

            connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
            try:
                with patch.object(self.auth, "authenticate", side_effect=observe):
                    connection.putrequest("POST", path)
                    connection.putheader("Authorization", "Bearer " + mac["token"])
                    connection.putheader("Content-Type", "application/json")
                    connection.putheader("Content-Length", "2")
                    connection.endheaders()
                    connection.send(b"{")
                    self.assertTrue(checked.wait(2))
                    self.auth.revoke(mac["device"], mac["device"]["id"])
                    connection.send(b"}")
                    response = connection.getresponse()
                    self.assertEqual(response.status, 401)
                    response.read()
            finally:
                connection.close()
        self.assertFalse(self.log.exists())


class ModelMigrationTests(unittest.TestCase):
    def test_version_one_upgrade_keeps_messages_state_and_cursors(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "old.sqlite3"
            with closing(sqlite3.connect(path)) as db:
                db.execute("CREATE TABLE conversations(id TEXT PRIMARY KEY,title TEXT,created_at TEXT,updated_at TEXT,archived INTEGER)")
                db.execute("INSERT INTO conversations VALUES ('old','original','time','time',0)")
                db.execute("PRAGMA user_version=1")
                db.commit()
            with Store(path) as store:
                self.assertIsNone(store.get_conversation("old")["model"])
                message = store.append_message("old", "user", "preserved")
                store.save_agent_state("old", '{"state":"unchanged"}')
                cursor = store.list_events()[-1]["seq"]
                store.update_conversation("old", model="model-b")
                store.update_conversation("old", title="renamed")
                self.assertEqual(store.get_conversation("old")["model"], "model-b")
                store.update_conversation("old", model=None)
                self.assertIsNone(store.get_conversation("old")["model"])
                self.assertEqual(store.list_messages("old"), [message])
                self.assertEqual(store.get_agent_state("old"), '{"state":"unchanged"}')
                self.assertGreater(store.list_events()[-1]["seq"], cursor)


if __name__ == "__main__":
    unittest.main()
