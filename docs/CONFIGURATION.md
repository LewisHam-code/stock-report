# 配置

模板：`research-data/automation.example.json`。本地有效配置：`research-data/automation.json`；未创建时只读模板。不要提交有效配置，它可能含个人关注和自定义接口信息。

| 字段 | 初始值 | 含义 |
| --- | --- | --- |
| `automatic_enabled` | `false` | 首次启动不自动采集或付费解读 |
| `model_profiles` | `[]` | 无预设账户；在页面添加模型 |
| `watchlist` | `[]` | 无原作者关注清单 |
| `daily_api_calls` | `10` | 日常模型调用次数上限，包含分阶段请求，不等于报告份数；0 表示不限 |
| `analysis_per_run` | `1` | 每轮日常解读上限；0 表示不限 |
| `download_per_run` | `2` | 每轮下载上限；0 表示不限 |
| `interval_minutes` | `60` | 前台自动运行间隔 |
| `financial_reading_cost_policy.limit_cny` | `0.5` | 单份累计预估上限；以服务商实际账单为准 |
| `prefer_off_peak` | `false` | 默认不依赖可能变化的峰谷时段；可自行启用 |
| `narrative_refresh_days` | `0` | 仅新版本触发综合解读，避免周期性重复收费 |
| `quarter_workers` | `2` | 内部高级财季队列的并发设置，不等同普通流程并发 |

`provider`、`model` 是兼容默认字段，空模型列表不会因这两个字段而自动启用模型。账户权益与仓位字段只用于模拟风险计算，不连接账户，也不读取实际资产。

## 密钥

支持 `DEEPSEEK_API_KEY`、`OPENAI_API_KEY`、`ANTHROPIC_API_KEY`、`GEMINI_API_KEY` 环境变量或同名 `.env.local` 字段。网页保存模型时会为配置生成独立的本地密钥引用，密钥文件设置为仅当前用户可读写。不在截图、日志、问题反馈或 Git 提交中提供真实值。

不同服务商并不保证具有相同的结构输出、上下文长度、模型名称和价格。模型价格预估表与闲时规则需按官方资料维护；未知单价可能触发跳过，而不是默认免费。

## 本地服务

`tools/serve.py --port` 控制监听端口，固定监听 `127.0.0.1`。浏览器使用同源 `/api/`。环境变量 `STOCK_PORT` 用于 CLI 启动路径；`STOCK_FOREGROUND=1` 表示在进程内按间隔运行，不登记系统任务。不要通过反向代理直接公开设置服务。
