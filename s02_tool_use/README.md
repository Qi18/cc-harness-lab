# s02：Tool Use

这一章保持 s01 的 Agent Loop 不变，把一个 Bash 工具扩展为五个工具：

- `bash`：执行 shell 命令；
- `read_file`：读取文件，可限制行数；
- `write_file`：写入完整文件；
- `edit_file`：只替换第一次出现的文本；
- `glob`：按 pattern 查找文件。

工具描述放在 `TOOLS`，实现函数注册到 `TOOL_HANDLERS`：

```python
TOOL_HANDLERS = {
    "bash": run_bash,
    "read_file": run_read,
    "write_file": run_write,
    "edit_file": run_edit,
    "glob": run_glob,
}
```

Agent Loop 不再认识任何具体工具，只按模型返回的工具名查表执行。因此后面新增
工具只需要补充 schema、实现函数和一条注册映射，不需要修改循环。

## 路径边界

四个文件工具只允许访问 `CC_WORKDIR` 内部：

- 拒绝 `../` 逃逸；
- 拒绝工作目录外的绝对路径；
- 解析符号链接后再次检查真实路径；
- `glob` 拒绝绝对 pattern 和包含 `..` 的 pattern。

本章还没有完整的 Bash 权限系统；这会在 s03 实现。

## 启动

```bash
cd /data/projects/cc-harness-lab
python s02_tool_use/code.py
```

建议测试：

1. `Read README.md and summarize it.`
2. `Create hello.py, then read it back.`
3. `Replace hello in hello.py with hi.`
4. `Find every Python file recursively.`
