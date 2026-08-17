# s04：Hooks

这一章把权限、日志、输出检查和退出控制挂到生命周期事件上。Agent Loop 只知道
事件名，不知道每个扩展具体做什么。

## Hook 注册表

```python
HOOKS = {
    "UserPromptSubmit": [],
    "PreToolUse": [],
    "PostToolUse": [],
    "Stop": [],
}
```

`register_hook(event, callback)` 按顺序注册回调，`trigger_hooks()` 按注册顺序执行。
回调返回 `None` 表示继续，返回其他值表示提前结束当前 Hook 链。

## 四个事件

| 事件 | 触发位置 | 本章默认 Hook |
| --- | --- | --- |
| `UserPromptSubmit` | 用户输入后、进入模型前 | 输出当前工作目录 |
| `PreToolUse` | handler 执行前 | 权限检查、调用日志 |
| `PostToolUse` | handler 执行后 | 大输出提醒 |
| `Stop` | 最终答案返回前 | 工具调用统计 |

`PreToolUse` 返回非 `None` 时，本次 handler 不会执行，返回值作为工具结果喂回
模型。`Stop` 返回非 `None` 时，该值作为一条新的 user 消息加入上下文，Agent
继续下一轮。

## OpenAI-compatible 适配

OpenAI-compatible 的工具参数是 JSON 字符串。本章先解析参数，再构造统一对象：

```python
@dataclass(frozen=True)
class ToolRequest:
    name: str
    arguments: dict[str, Any]
```

Hook 只接触 `ToolRequest`，不依赖百炼或 OpenAI SDK 的响应对象。以后增加其他模型
协议时，只需要在适配层构造同样的对象。

## 权限如何迁移

s03 的 `check_permission()` 逻辑没有删除，而是被包装为：

```python
def permission_hook(request):
    allowed, reason = check_permission(request.name, request.arguments)
    return None if allowed else f"Permission denied: {reason}"
```

然后注册到 `PreToolUse`。执行器不再直接引用权限函数。

## 启动

```bash
cd /data/projects/cc-harness-lab
python s04_hooks/code.py
```

建议测试：

1. `Read README.md.`：观察输入、执行前和停止 Hook；
2. `Create hook-demo.txt.`：观察 PostToolUse；
3. `Delete hook-demo.txt using rm.`：由权限 Hook 询问；
4. `Run sudo whoami.`：权限 Hook 直接阻止。

## 教学边界

本章 Hook 都是同步的进程内 Python 回调。Hook 抛异常会终止本轮执行，第三方 Hook
也没有超时、进程隔离或权限限制。生产系统还需要 Hook 配置加载、matcher、错误
隔离、超时、可观测结果和安全规则不可绕过等机制。
