# s03：Permission

这一章在 s02 的五工具分发前增加权限判断。模型只负责提出工具请求，是否执行由
Harness 决定。

## 三道闸门

权限判断顺序固定：

1. `check_deny_list`：硬拒绝危险 Bash 命令；
2. `check_rules`：识别需要人工审批的风险操作；
3. `ask_user`：命中软规则后暂停，默认拒绝。

没有命中拒绝或询问规则的工具直接放行。

```text
tool call
   -> deny list 命中？ -> deny
   -> ask rule 命中？ -> user y/yes -> allow
                              other -> deny
   -> allow
```

## 当前规则

硬拒绝包括：

- 删除根目录；
- `sudo`、关机和重启；
- `mkfs`、`dd if=`；
- 写入 `/dev/`。

需要询问包括：

- Bash `rm`；
- 重定向写入 `/etc/`；
- `chmod 777`；
- `write_file` 或 `edit_file` 请求工作目录外路径。

审批并不会绕过 s02 的 `safe_path`。例如用户允许一次工作目录外写入后，文件工具
仍会因为路径边界拒绝访问；权限决定和执行能力是两层不同的约束。

## 启动

```bash
cd /data/projects/cc-harness-lab
python s03_permission/code.py
```

建议测试：

1. `Read README.md.`：直接放行；
2. `Create test.txt in the current directory.`：直接放行；
3. `Delete test.txt using rm.`：询问用户；
4. `Run sudo whoami.`：硬拒绝，不询问。

## 教学边界

本章使用字符串和少量正则匹配帮助理解权限路由，并不是完整 Shell 安全解析器。
命令可以通过别名、变量展开或其他写法变形。生产级 Harness 还需要结构化命令
分析、规则来源优先级、会话授权、Hook 和沙箱隔离。
