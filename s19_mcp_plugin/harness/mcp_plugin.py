"""s19 教学版 MCP 工具桥：进程内模拟发现与调用，不实现传输层。"""

from __future__ import annotations

import copy
import json
import re
import threading
from collections.abc import Callable
from typing import Any

from .task_system import _task_tool


CONNECT_MCP_TOOL = _task_tool(
    "connect_mcp",
    "Connect a teaching mock MCP server (docs or deploy); no external service is contacted.",
    {"name": {"type": "string"}}, ["name"],
)


def normalize_mcp_name(name: str) -> str:
    """规范化模型可见名称；冲突必须另行检测，替换字符本身不能保证唯一。"""
    if not isinstance(name, str) or not name.strip():
        raise ValueError("MCP name must be a non-empty string")
    return re.sub(r"[^a-zA-Z0-9_-]", "_", name)


class MCPClient:
    """模拟 tools/list 和 tools/call；后续可用真正传输层替换，不冒充 JSON-RPC。"""

    def __init__(self, name: str):
        normalize_mcp_name(name)
        self.name = name
        self._tools: list[dict[str, Any]] = []
        self._handlers: dict[str, Callable[..., Any]] = {}

    @property
    def tools(self) -> list[dict[str, Any]]:
        # 返回快照，外部修改 schema 不能悄悄改变已登记能力。
        return copy.deepcopy(self._tools)

    def register(self, tool_defs: list[dict], handlers: dict[str, Callable]) -> None:
        names = set()
        for tool in tool_defs:
            name = tool["name"]
            normalize_mcp_name(name)
            if name in names or not callable(handlers.get(name)):
                raise ValueError("duplicate tool or missing handler")
            names.add(name)
            schema = tool.get("inputSchema")
            if not isinstance(schema, dict) or schema.get("type") != "object":
                raise ValueError("inputSchema must describe an object")
            if not isinstance(tool.get("description", ""), str):
                raise ValueError("tool description must be text")
            annotations = tool.get("annotations", {})
            if not isinstance(annotations, dict):
                raise ValueError("annotations must be an object")
            for hint in ("readOnlyHint", "destructiveHint"):
                if hint in annotations and type(annotations[hint]) is not bool:
                    raise ValueError("annotation hints must be booleans")
        # 在所有记录校验成功后才替换，避免部分登记。
        json.dumps(tool_defs, allow_nan=False)
        self._tools = copy.deepcopy(tool_defs)
        self._handlers = {name: handlers[name] for name in names}

    def call_tool(self, tool_name: str, args: dict) -> str:
        handler = self._handlers.get(tool_name)
        if handler is None:
            return f"MCP error: unknown tool {tool_name!r}"
        if not isinstance(args, dict):
            return "MCP error: arguments must be an object"
        try:
            result = handler(**args)
            return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
        except Exception as error:
            # handler 失败是本次工具结果，不让异常中断整个 Agent turn。
            return f"MCP error: {type(error).__name__}: {error}"


def _schema(name: str, description: str, properties: dict, required: list[str],
            *, read_only: bool) -> dict:
    return {
        "name": name, "description": description,
        "inputSchema": {"type": "object", "properties": properties,
                        "required": required, "additionalProperties": False},
        "annotations": {"readOnlyHint": read_only, "destructiveHint": not read_only},
    }


def docs_server() -> MCPClient:
    """固定文档数据集，任何查询都不会读取用户文件或访问网络。"""
    client = MCPClient("docs")
    documents = {
        "worktree": "Worktrees isolate task directories; completing a task does not merge branches.",
        "mcp": "Connect, discover tools, assemble schemas, then route calls through the MCP bridge.",
    }

    def search(query: str):
        if not isinstance(query, str):
            raise ValueError("query must be a string")
        return {"mock": True, "results": [
            {"id": key, "text": value} for key, value in documents.items()
            if query.lower() in (key + " " + value).lower()
        ]}

    def get(document_id: str):
        if document_id not in documents:
            raise ValueError("unknown document")
        return {"mock": True, "id": document_id, "text": documents[document_id]}

    client.register([
        _schema("search", "Search the built-in mock documentation dataset.",
                {"query": {"type": "string"}}, ["query"], read_only=True),
        _schema("get", "Get one mock document by ID.",
                {"document_id": {"type": "string"}}, ["document_id"], read_only=True),
    ], {"search": search, "get": get})
    return client


def deploy_server() -> MCPClient:
    """演示带副作用标注的工具；只更改当前连接的内存记录，不执行真实部署。"""
    client = MCPClient("deploy")
    records: dict[str, str] = {}

    def deploy(service: str):
        if not isinstance(service, str) or not service.strip():
            raise ValueError("service must be a non-empty string")
        records[service] = "simulated"
        return {"mock": True, "service": service, "status": "simulated",
                "notice": "No deployment was performed."}

    def status(service: str):
        if not isinstance(service, str):
            raise ValueError("service must be a string")
        return {"mock": True, "service": service, "status": records.get(service, "not_started")}

    properties = {"service": {"type": "string"}}
    client.register([
        _schema("deploy", "Simulate deployment in memory; never contacts a real deployment system.",
                properties, ["service"], read_only=False),
        _schema("status", "Read simulated deployment status.",
                properties, ["service"], read_only=True),
    ], {"deploy": deploy, "status": status})
    return client


MOCK_SERVERS = {"docs": docs_server, "deploy": deploy_server}


class MCPManager:
    """连接只认受信任代码登记的 factory；模型不能传入 URL、命令或密钥。"""

    def __init__(self, factories: dict[str, Callable[[], MCPClient]] | None = None):
        self.factories = dict(MOCK_SERVERS if factories is None else factories)
        self._clients: dict[str, MCPClient] = {}
        self._lock = threading.RLock()

    @staticmethod
    def _assemble(clients, builtin_tools, builtin_handlers):
        tools = copy.deepcopy(builtin_tools)
        handlers = dict(builtin_handlers)
        # schema 和 handler 两侧都要查重，不覆盖任何已有能力。
        used = {t["function"]["name"] for t in tools} | set(handlers)
        for server_name, client in clients.items():
            for definition in client.tools:
                original = definition["name"]
                name = f"mcp__{normalize_mcp_name(server_name)}__{normalize_mcp_name(original)}"
                if len(name) > 64 or name in used:
                    raise ValueError(f"MCP tool name collision or length exceeds 64: {name}")
                used.add(name)
                hints = definition.get("annotations", {})
                labels = []
                if hints.get("readOnlyHint"):
                    labels.append("(readOnly)")
                if hints.get("destructiveHint"):
                    labels.append("(destructive)")
                tools.append({"type": "function", "function": {
                    "name": name,
                    "description": " ".join(labels + [definition.get("description", "")]),
                    "parameters": copy.deepcopy(definition["inputSchema"]),
                }})
                # 工厂函数捕获当前 client 与原始名称，避免 for 循环闭包晚绑定。
                def bind(c, tool_name):
                    return lambda **kwargs: c.call_tool(tool_name, kwargs)
                handlers[name] = bind(client, original)
        return tools, handlers

    def assemble_tool_pool(self, builtin_tools, builtin_handlers):
        with self._lock:
            return self._assemble(self._clients, builtin_tools, builtin_handlers)

    def connect_mcp(self, name: str, builtin_tools=(), builtin_handlers=None) -> str:
        with self._lock:
            try:
                safe = normalize_mcp_name(name)
                if name in self._clients:
                    return f"MCP server {name!r} already connected"
                if any(normalize_mcp_name(n) == safe for n in self._clients):
                    raise ValueError("normalized server name collision")
                factory = self.factories.get(name)
                if factory is None:
                    return "Unknown MCP server. Available: " + ", ".join(sorted(self.factories))
                client = factory()
                if not isinstance(client, MCPClient):
                    raise ValueError("factory must return MCPClient")
                candidate = {**self._clients, name: client}
                # 先完整组装校验，成功才发布连接；失败不会污染已连接工具池。
                tools, _ = self._assemble(candidate, list(builtin_tools), builtin_handlers or {})
                self._clients = candidate
                discovered = [t["function"]["name"] for t in tools
                              if t["function"]["name"].startswith(f"mcp__{safe}__")]
                return f"Connected to mock MCP server {name!r}. Discovered: " + ", ".join(discovered)
            except Exception as error:
                return f"MCP error: {type(error).__name__}: {error}"
