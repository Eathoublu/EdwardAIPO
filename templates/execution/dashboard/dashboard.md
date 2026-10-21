# Dashboard

> 由固定采集脚本生成。查数字：`cat edward/execution/dashboard/data/latest.json`；
> 刷新：`.opencode/bin/edward collect`。

- 更新时间：尚未采集
- 采集状态：ok=0 / failed=0 / skipped=0

## 指标

尚未配置指标。

1. 编辑 `edward/execution/dashboard/metrics.json`。
2. 在 `edward/execution/dashboard/collectors/` 写固定采集脚本。
3. 运行 `.opencode/bin/edward collect`。

## 查询入口

- 机器可读最新值：`edward/execution/dashboard/data/latest.json`
- 历史快照：`edward/execution/dashboard/data/history/`
- 指标定义：`edward/execution/dashboard/metrics.json`
- 固定采集脚本：`edward/execution/dashboard/collectors/`
