# AndyKong 的 Claude Code 插件市场

这是我的个人 Claude Code 插件市场，包含了我开发和收藏的有用插件。

## 安装此市场

在 Claude Code 中运行以下命令来添加此市场：

```bash
/plugin marketplace add AndyKong2020/my-first-marketplace
```

## 安装插件

添加市场后，你可以安装其中的插件：

```bash
/plugin install example-plugin@my-first-marketplace
/plugin install claude-session-log@my-first-marketplace
```

## 更新市场

当市场更新后，运行以下命令获取最新插件：

```bash
/plugin marketplace update my-first-marketplace
```

## 插件列表

### example-plugin
- **描述**: 一个综合示例插件，展示所有 Claude Code 扩展选项
- **版本**: 1.0.0
- **分类**: examples

### claude-session-log
- **描述**: 把 Claude Code 的 transcript 与 telemetry 增量同步为项目内日志，并生成 `summary/<session_id>/` + `meta/` 详细产物
- **版本**: 0.3.0
- **分类**: utilities

## 贡献

欢迎提交问题和建议！

## 许可证

MIT License
