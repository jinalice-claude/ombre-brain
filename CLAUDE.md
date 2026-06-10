# ombre-brain 项目约定

## 计划信箱（plan 投递）

每次生成需要瑾儿审阅的 plan 后，主动把 plan 全文投递到计划信箱，然后告知瑾儿「已投递」，由手机端克克通过 GET 读取审阅。

- 投递方式：`POST /api/public/plan`，请求头带 `X-Public-Token`（环境变量 `OMBRE_PUBLIC_TOKEN`，绝不硬编码），请求体 `{"content": "<plan 全文>"}`。
- 读取方式：`GET /api/public/mailbox`（`/api/public/plan` 的 GET 别名，绕开手机端抓取代理的旧缓存），无鉴权，返回纯文本；信箱只保留最新一份。
- content 上限 50000 字符，超长先精简再投递。

## 本项目红线

- 不碰记忆桶逻辑（bucket_manager）、衰减引擎、嵌入引擎和任何现有接口，除非瑾儿明确要求。
- `buckets_dir` 下的 `permanent/`、`dynamic/`、`archive/`、`feel/` 是记忆桶数据，绝不直接增删改其中的文件；新功能的数据文件放独立子目录（如 `mailbox/`），且不要用 `.md` 后缀，避免被桶扫描误读。
- 推送到 main 即触发 Zeabur 自动部署，push 前必须经瑾儿确认。
