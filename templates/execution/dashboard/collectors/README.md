# Collectors · 固定采集脚本

目标：**一个指标一个固定脚本，任何时候运行同一条命令都能拿到数字**。

## 契约

- 脚本从 `metrics.json` 的 `collector` 字段启动。
- stdout 只输出一个 JSON 对象：

```json
{"id":"mrr","value":12345,"unit":"CNY","source":"https://...","observed_at":"2026-10-18T09:00:00+08:00"}
```

- 诊断/日志写 stderr，不要污染 stdout。
- 失败时退出码非 0。
- 不在脚本里写密钥；用环境变量或 `metrics.json` 的 `env`。

## 网页指标

1. 复制 `web_metric.py` 为 `mrr.py` 等固定脚本。
2. 修改 `METRIC_URL` / `METRIC_SELECTOR`，或通过 `metrics.json.env` 传入。
3. 需要登录态时：
   ```bash
   export PLAYWRIGHT_STORAGE_STATE=/absolute/path/to/state.json
   ```
   登录态文件不要提交。
4. 首次使用 Playwright 浏览器：
   ```bash
   python3 -m playwright install chromium
   ```
5. 在 `metrics.json` 注册，并在项目根目录验证：

```bash
python3 edward/execution/dashboard/collectors/mrr.py
.opencode/bin/edward collect
cat edward/execution/dashboard/data/latest.json
```

## 命令 / API 指标

复制 `command_metric.py`，在 `metrics.json.env.METRIC_COMMAND` 里放稳定的
curl / CLI / 脚本命令。它会把 JSON、裸数字或文本中的第一个数字统一成
`{"value": ...}`。

## 规则

- 禁止每个班次临时手写爬虫；必须固化到本目录。
- 网页结构变化时，更新脚本，不要改一次性的临时命令。
- 指标口径变化时，同时更新 `goal.md` 和 `metrics.json`。
