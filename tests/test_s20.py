"""s20 集成验收：复用 s19 实现，不复制章节 Harness；不调用付费模型。"""

import copy
from contextlib import ExitStack
import json
import subprocess
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from s19_mcp_plugin.harness import AgentHarness, Settings, memory
from s19_mcp_plugin.harness.mcp_plugin import MCPClient


def tool(name, arguments=None, call_id=None):
    return SimpleNamespace(
        id=call_id or name, type="function",
        function=SimpleNamespace(name=name, arguments=json.dumps(arguments or {})),
    )


def response(content=None, calls=()):
    """生成与 SDK 一致的可序列化消息，确保归档中的 tool_calls 不是对象字符串。"""
    payload = {"role": "assistant", "content": content}
    if calls:
        payload["tool_calls"] = [
            {"id": c.id, "type": "function",
             "function": {"name": c.function.name, "arguments": c.function.arguments}}
            for c in calls
        ]
    message = SimpleNamespace(content=content, tool_calls=list(calls),
                              model_dump=lambda **_: copy.deepcopy(payload))
    return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="stop")])


class Client:
    def __init__(self, callback):
        self.callback = callback
        self.requests = []
        self.lock = threading.Lock()
        self.chat = SimpleNamespace(completions=self)

    def create(self, **kwargs):
        with self.lock:
            self.requests.append(copy.deepcopy(kwargs))
        return self.callback(kwargs)


class IntegratedTurnTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cc-s20-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = Settings(
            workdir=self.root, skills_dir=self.root/"skills",
            transcript_dir=self.root/".transcripts",
            tool_results_dir=self.root/".task_outputs",
            memory_dir=self.root/".memory", tasks_dir=self.root/".tasks",
        )
        # 只替换模型驱动的 Memory 选择/提取，不掩盖 Hook、工具或任务系统逻辑。
        self.contexts = ExitStack()
        self.addCleanup(self.contexts.close)
        self.recall = self.contexts.enter_context(patch.object(memory, "load_memories", return_value=[]))
        self.extract = self.contexts.enter_context(patch.object(memory, "extract_memories"))
        self.contexts.enter_context(patch.object(memory, "consolidate_memories"))

    def make(self, callback):
        client = Client(callback)
        h = AgentHarness(client, self.settings, sleep_fn=lambda _:None,
                         random_fn=lambda a,b:a)
        self.addCleanup(h.stop_cron_runtime)
        return h, client

    def git(self, *args, cwd=None):
        return subprocess.run(["git",*args], cwd=cwd or self.root,
                              capture_output=True,text=True,check=True).stdout.strip()

    def init_git(self):
        self.git("init","-b","main")
        self.git("config","user.name","Integration Test")
        self.git("config","user.email","integration@example.invalid")
        (self.root/"shared.txt").write_text("baseline\n")
        self.git("add","shared.txt")
        self.git("commit","-qm","baseline")

    def assert_protocol(self, messages):
        """每个请求和归档都不能有孤立、重复或缺失的 tool result。"""
        pending = set()
        for m in messages:
            if m["role"] == "tool":
                self.assertIn(m["tool_call_id"],pending)
                pending.remove(m["tool_call_id"])
            else:
                self.assertFalse(pending, "tool batch split before results completed")
                if m["role"] == "assistant":
                    pending = {t["id"] for t in m.get("tool_calls",[])}
        self.assertFalse(pending)

    def test_worktree_team_mcp_background_and_compaction_share_one_turn(self):
        self.init_git()
        claimed = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)
        lead_round = 0
        summaries = []

        def model(request):
            nonlocal lead_round
            if "tools" not in request:
                summaries.append(json.loads(request["messages"][1]["content"]))
                return response("MCP connected, search returned; teammate is working; retain goal.")
            if threading.current_thread().name.startswith("cc-teammate"):
                events=[m for m in request["messages"] if "[Auto-claimed task]" in str(m.get("content"))]
                if not events:
                    return response("READY")
                task=json.loads(events[-1]["content"].split("\n",1)[1])
                if not any(m["role"]=="tool" for m in request["messages"]):
                    claimed.set()
                    if not release.wait(3):
                        raise RuntimeError("test did not release teammate")
                    return response(calls=[
                        tool("write_file",{"path":"shared.txt","content":"isolated\n"}),
                        tool("read_file",{"path":"shared.txt"}),
                        tool("complete_task",{"task_id":task["id"]}),
                    ])
                return response("TEAM_DONE")
            lead_round += 1
            if lead_round == 1:
                self.assertTrue(claimed.wait(3))
                return response(calls=[
                    tool("connect_mcp",{"name":"docs"}),
                    tool("bash",{"command":"printf background-ok","run_in_background":True}),
                    tool("mcp__docs__search",{"query":"worktree"}),
                    tool("compact"),
                ])
            if lead_round == 2:
                release.set()
                self.assertTrue(h.team.wait("alice",3))
                self.assertTrue(h.background.wait("bg_1",3))
                return response("premature final")  # 到达的通知必须让 Lead 再跑一轮。
            return response("ALL_DONE")

        h, client = self.make(model)
        h.team.autonomy.idle_timeout=.05
        h.team.idle_poll_interval=.01
        h.tasks.create_task("isolated work")
        task=h.tasks._all()[0]
        self.assertIn("created",h.worktrees.create_worktree("alpha",task.id))
        self.addCleanup(lambda:h.team.wait("alice",3))
        h.team.spawn_teammate("alice","developer","Reply READY and wait for auto claim.")
        messages=[{"role":"user","content":"Verify all results, preserve worktree."}]
        try:
            self.assertEqual("ALL_DONE",h.run_turn(messages))
        finally:
            release.set()
            h.team.wait("alice",3)
        self.assertEqual("baseline\n",(self.root/"shared.txt").read_text())
        self.assertEqual("isolated\n",(self.root/".worktrees/alpha/shared.txt").read_text())
        self.assertEqual("completed",h.tasks._load(task.id).status)
        self.assertTrue(summaries)
        for request in client.requests:
            if "tools" in request:
                self.assert_protocol(request["messages"])
        for transcript in self.settings.transcript_dir.glob("*.jsonl"):
            self.assert_protocol([json.loads(l) for l in transcript.read_text().splitlines()])
        lead_requests=[r for r in client.requests if any(t["function"]["name"]=="connect_mcp" for t in r.get("tools",[]))]
        self.assertNotIn("compact",{t["function"]["name"] for t in lead_requests[-1]["tools"]})
        self.assertIn("mcp__docs__search",{t["function"]["name"] for t in lead_requests[-1]["tools"]})
        self.assertIn("TEAM_DONE",str(lead_requests[-1]["messages"]))
        combined=summaries[0]+messages
        notices=[m for m in combined if m["role"]=="user" and "<task_notification" in str(m.get("content"))]
        self.assertEqual(1,len(notices))
        self.assertFalse(h.worktrees._users.get("alpha"))
        self.assertEqual([],h.team.consume_lead_messages())
        self.extract.assert_called_once()

    def test_mcp_survives_rate_limit_without_reexecuting_connect(self):
        class RateLimit(RuntimeError):
            status_code=429
        step=0
        def model(request):
            nonlocal step
            step+=1
            if step==1:return response(calls=[tool("connect_mcp",{"name":"docs"})])
            if step==2:raise RateLimit("rate limited")
            if step==3:return response(calls=[tool("mcp__docs__get",{"document_id":"mcp"})])
            return response("done")
        h,client=self.make(model)
        with patch.object(h.mcp,"connect_mcp",wraps=h.mcp.connect_mcp) as connect:
            self.assertEqual("done",h.run_turn([{"role":"user","content":"Read MCP docs."}]))
            connect.assert_called_once()
        self.assertEqual(client.requests[1]["tools"],client.requests[2]["tools"])
        self.assertEqual(client.requests[1]["messages"],client.requests[2]["messages"])
        for r in client.requests:self.assert_protocol(r["messages"])

    def test_cron_failure_restores_queue_without_losing_mcp_connection(self):
        fail=True
        def model(request):
            if fail:raise RuntimeError("invalid request")
            return response("scheduled done")
        h,client=self.make(model)
        h._connect_mcp("docs")
        job=h.cron.schedule_job("* * * * *","scheduled doc check",recurring=False,durable=False)
        h.cron.poll_due_jobs(datetime(2026,9,5,12,0))
        messages=[]
        self.assertIn("invalid request",h.run_turn(messages))
        self.assertEqual([job.id],[j.id for j in h.cron.queue])
        self.assertFalse(any("[Scheduled]" in str(m.get("content")) for m in messages))
        fail=False
        self.assertEqual("scheduled done",h.run_turn(messages))
        self.assertNotIn(job.id,h.cron.jobs)
        self.assertFalse(h.cron.has_queue())
        self.assertIn("mcp__docs__get",{t["function"]["name"] for t in client.requests[-1]["tools"]})

    def test_permission_denial_blocks_background_but_not_other_batch_tools(self):
        step=0
        def model(request):
            nonlocal step
            step+=1
            if step==1:return response(calls=[
                tool("connect_mcp",{"name":"docs"}),
                tool("bash",{"command":"printf denied","run_in_background":True}),
                tool("mcp__docs__get",{"document_id":"mcp"}),
            ])
            return response("denial respected")
        h,client=self.make(model)
        h.hooks.register("PreToolUse",lambda req:"Denied by integration policy" if req.name=="bash" else None)
        messages=[{"role":"user","content":"Respect policy."}]
        self.assertEqual("denial respected",h.run_turn(messages))
        self.assertEqual((),h.background.snapshot())
        results=[m for m in messages if m["role"]=="tool"]
        self.assertEqual(3,len(results))
        self.assertIn("Denied",results[1]["content"])
        self.assertEqual("mcp",json.loads(results[2]["content"])["id"])
        self.assert_protocol(messages)

    def test_stop_hook_continuation_extracts_memory_only_after_final_answer(self):
        step=0
        def model(request):
            nonlocal step
            step+=1
            if step==1:return response("first answer")
            if step==2:return response(calls=[tool("connect_mcp",{"name":"docs"})])
            return response("verified answer")
        h,_=self.make(model)
        stops=[]
        def stop(messages):
            stops.append(len(messages))
            return "Check docs before stopping." if len(stops)==1 else None
        h.hooks.register("Stop",stop)
        self.assertEqual("verified answer",h.run_turn([{"role":"user","content":"Verify."}]))
        self.assertEqual(2,len(stops))
        self.extract.assert_called_once()
        self.assertIn("verified answer",str(self.extract.call_args))

    def test_large_mcp_result_is_persisted_before_next_model_request(self):
        large="integration-result-"*2000
        step=0
        def factory():
            c=MCPClient("large")
            c.register([{"name":"read","inputSchema":{"type":"object"}}],{"read":lambda:large})
            return c
        def model(request):
            nonlocal step
            step+=1
            if step==1:return response(calls=[tool("connect_mcp",{"name":"large"})])
            if step==2:return response(calls=[tool("mcp__large__read")])
            return response("large output received")
        h,client=self.make(model)
        h.mcp.factories["large"]=factory
        h.compactor.TOOL_RESULT_BATCH_CHAR_LIMIT=4000
        self.assertEqual("large output received",h.run_turn([{"role":"user","content":"Read large."}]))
        output=next(m for m in client.requests[-1]["messages"] if m.get("tool_call_id")=="mcp__large__read")
        self.assertIn("<persisted-output>",output["content"])
        self.assertEqual([large],[p.read_text() for p in self.settings.tool_results_dir.glob("*.txt")])
        self.assert_protocol(client.requests[-1]["messages"])

    def test_cron_runtime_uses_connected_tools_and_releases_threads(self):
        h,client=self.make(lambda request:response("scheduled result"))
        h._connect_mcp("docs")
        h.cron.schedule_job("* * * * *","inspect docs",recurring=False,durable=False)
        h.cron.poll_due_jobs(datetime(2026,9,5,12,0))
        messages=[]
        delivered=threading.Event()
        h._agent_lock.acquire()
        try:
            h.start_cron_runtime(messages,lambda answer:delivered.set())
            self.assertFalse(delivered.wait(.25))
            self.assertEqual([],client.requests)
        finally:
            h._agent_lock.release()
        queue_thread = h._queue_thread
        try:
            self.assertTrue(delivered.wait(3))
        finally:
            h.stop_cron_runtime()
        h.stop_cron_runtime()
        self.assertFalse(h.cron.is_running)
        self.assertFalse(queue_thread.is_alive())
        self.assertIn("mcp__docs__search",{t["function"]["name"] for t in client.requests[0]["tools"]})
        self.assert_protocol(messages)

    def test_recalled_memory_and_cron_survive_compact_without_replacing_user_goal(self):
        self.recall.return_value="Historical preference: use mock documentation."
        step=0
        def model(request):
            nonlocal step
            if "tools" not in request:
                return response("The scheduled check and historical preference are background.")
            step+=1
            if step==1:return response(calls=[tool("compact")])
            return response("human goal retained")
        h,client=self.make(model)
        h.cron.schedule_job("* * * * *","scheduled background goal",recurring=False,durable=False)
        h.cron.poll_due_jobs(datetime(2026,9,5,12,0))
        messages=[{"role":"user","content":"Human goal: inspect, do not deploy."}]
        self.assertEqual("human goal retained",h.run_turn(messages,active_request="Human goal: inspect, do not deploy."))
        self.assertIn("Background knowledge only",str(client.requests[0]["messages"]))
        compacted=next(m["content"] for m in client.requests[-1]["messages"] if "[Compacted]" in str(m.get("content")))
        self.assertIn("Current user request:\nHuman goal: inspect, do not deploy.",compacted)
        self.assert_protocol(client.requests[-1]["messages"])

    def test_missing_bound_worktree_fails_worker_without_completing_task(self):
        self.init_git()
        h,client=self.make(lambda request:response("READY"))
        h.tasks.create_task("missing workspace")
        task=h.tasks._all()[0]
        # 模拟绑定后目录被外部操作移除：不实际删除任何用户或 Git 工作区。
        h.tasks.bind_task_to_worktree(task.id,"missing")
        h.team.idle_poll_interval=.01
        h.team.autonomy.idle_timeout=.05
        h.team.spawn_teammate("alice","developer","Wait for assigned task.")
        self.assertTrue(h.team.wait("alice",3))
        self.assertEqual("failed",h.team.snapshot()[0].status)
        self.assertEqual("in_progress",h.tasks._load(task.id).status)
        self.assertEqual("baseline\n",(self.root/"shared.txt").read_text())
        # 队友失败只通过邮箱向 Lead 汇报，不污染 Lead 的 MCP 连接能力。
        self.assertIn("Connected",h._connect_mcp("docs"))
        h.client=Client(lambda request:response("worker failure observed"))
        self.assertEqual("worker failure observed",h.run_turn([{"role":"user","content":"Inspect failure."}]))
        self.assertIn("not managed",str(h.client.requests[0]["messages"]))


if __name__ == "__main__":
    unittest.main()
