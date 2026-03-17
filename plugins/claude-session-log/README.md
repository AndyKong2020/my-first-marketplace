# claude-session-log

`claude-session-log` 是一个 Claude Code 插件。它通过 hooks 持续同步当前会话的 transcript 与 telemetry，并把结果落到项目内的 `./.claude-log`。

## 功能

- 为每个 session 生成独立摘要目录：`./.claude-log/summary/<yyyy-mm-dd_hh-mm-ss>/`
- 在每个 session 摘要目录下写出：
  - `summary.md`
  - `usage.json`
- 把完整详细产物收拢到：`./.claude-log/meta/`
- 按会话生成详细 Markdown：`./.claude-log/meta/sessions/YYYY/MM/<session_id>.md`
- 生成详细索引：`./.claude-log/meta/index.md`
- 记录同步状态：`./.claude-log/meta/state/<session_id>.json`
- 为大文本、结构化对象和 base64 图片生成侧写文件：`./.claude-log/meta/artifacts/<session_id>/`
- 合并当前 session 的 telemetry，补充 token、cost、duration、TTFT 等指标
- 自动并入同一主 session 下的 subagent transcript
- `summary.md` 默认保留必要对话、`thinking`、模型输出和工具调用记录

## 安装

先添加 marketplace：

```bash
/plugin marketplace add AndyKong2020/my-first-marketplace
```

然后安装插件：

```bash
/plugin install claude-session-log@my-first-marketplace
```

启用后，新启动的 Claude Code 会话会在关键 hook 事件上持续刷新 `./.claude-log`。

## 输出目录

```text
.claude-log/
├── summary/<yyyy-mm-dd_hh-mm-ss>/
│   ├── summary.md
│   └── usage.json
└── meta/
    ├── index.md
    ├── sessions/YYYY/MM/<session_id>.md
    ├── state/<session_id>.json
    └── artifacts/<session_id>/
        ├── telemetry.jsonl
        └── rendered/
```

其中：

- `summary/<yyyy-mm-dd_hh-mm-ss>/summary.md` 是该 session 的人类可读入口，只保留主线信息
- `summary/<yyyy-mm-dd_hh-mm-ss>/usage.json` 是该 session 的结构化用量统计，适合脚本或看板读取
- `summary/` 目录名默认取 session 首条事件时间，格式为 `YYYY-MM-DD_HH-MM-SS`（北京时间，UTC+8）
- `meta/` 保存完整 transcript、telemetry、state 和侧写文件，适合调试与取证

## 调试

运行单元测试：

```bash
python3 -m unittest discover -s tests
```

手动用 JSON 文件调试同步脚本：

```bash
python3 scripts/sync_session_log.py --hook-input /path/to/hook-input.json
```

也可以显式传参：

```bash
python3 scripts/sync_session_log.py \
  --project-dir /path/to/workspace \
  --session-id SESSION_ID \
  --transcript /path/to/session.jsonl \
  --telemetry-dir /path/to/telemetry
```

插件内部错误会写入：

```text
.claude-log/plugin-errors.log
```

## 已知限制

- 首版只保证插件启用后的新会话/新事件持续同步，不做历史全量回填。
- telemetry 合并是 best-effort，只吸收 `session_id` 匹配的事件。
- 大 payload 会被拆到 `meta/artifacts/`，Markdown 里只保留链接与摘要。
- 本次版本不再继续写旧的顶层 `summary.md` / `usage.json`，会改为 `summary/<yyyy-mm-dd_hh-mm-ss>/...`。
- 本次版本把旧的顶层 `index.md`、`sessions/`、`state/`、`artifacts/` 迁到了 `meta/` 下，不再继续写旧路径。
- 修改 `hooks/hooks.json` 或脚本后，需要重启 Claude Code 会话，新的 hook 配置才会生效。
