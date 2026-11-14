# API 与配置

[返回项目首页](../README.md) · [部署与备份](DEPLOYMENT.md)

## 账号格式

把每个账号保存为 `accounts/*.json`。支持以下任一形式：

```json
{
  "id": "account-1",
  "device_id": "your-device-id",
  "web_id": "your-web-id",
  "cookies": {
    "sessionid": "...",
    "passport_csrf_token": "..."
  }
}
```

也可以使用 `"cookie": "sessionid=...; passport_csrf_token=..."`，或从浏览器
导出的 cookie 数组。`device_id` 通常来自
`localStorage.__tea_cache_tokens_1044603.user_unique_id`，`web_id` 来自同一
对象的 `web_id`。

账号目录默认是 `accounts/`，已被 `.gitignore` 排除。不要把账号文件提交到
GitHub，也不要贴到聊天中。

如果本机已有登录过的豆包工作电脑版，可以直接在控制台点「离线扫描」：

Windows 离线扫描会读取客户端留下的 Cookie 数据库和 Local Storage，
使用当前 Windows 用户的 DPAPI 解密，然后写入本地 `accounts/`。
客户端必须关闭，否则 Chromium 会独占 Cookie 数据库。读取完成后所有
临时文件和数据库句柄立即释放，不要求客户端常驻，也不需要开放 `9222`。

```text
1. 登录一次豆包工作电脑版
2. 完全退出客户端
3. 打开控制台 → 账号 → 离线扫描
```

离线扫描不需要安装 Playwright、启动调试端口或让客户端常驻。
Playwright 不属于正式运行依赖，也不会随基础安装或 wheel 安装。


## 工具调用与思考强度

`POST /v1/chat/completions` 支持 OpenAI `tools`（`type: "function"`）、
`tool_choice`（`auto`、`none`、`required` 或指定函数）与 `parallel_tool_calls`。
工具定义会转换为提示词，模型按约定输出结构化文本后，网关校验并返回
`message.tool_calls` 和 `finish_reason: "tool_calls"`。工具由调用方执行，
随后以 `role: "tool"`、`tool_call_id` 回传；网关保留调用 ID、名称和参数历史。
这属于文本桥接，效果取决于模型遵循约定的能力，不等同于上游原生工具协议。
工具请求的答案先缓冲校验后再发送 SSE；普通文本请求仍即时增量返回。

工具历史按调用 ID 配对；兼容旧式 `functions` / `function_call` 和函数结果，
对孤立、冲突或有歧义的历史明确报错。参数格式容错仅用于结构化工具信封，
不会执行模型返回的代码。思考历史保留原文并单独标记回放；响应以
`reasoning_content` 和 `reasoning` 镜像暴露上游实际返回的可见思考，不生成隐藏推理。

内置模型目录的能力快照如下；账号实时目录可能更新默认档位，以面板显示为准：

| 模型 | 总窗口 | `reasoning_effort` | 默认 |
| --- | ---: | --- | --- |
| 豆包 2.1 Lite | 256,000 | `low / medium / high` | `high` |
| DeepSeek Flash、DeepSeek V4 Pro、三个 Kimi | 300,000 | `low / medium / high / xhigh / max` | `max` |
| GLM 5.3、GLM 5.3 Flash | 300,000 | `low / high / max` | `max` |

各档分别映射官方整数 `3 / 4 / 5 / 6 / 7`；不支持的档位明确报错。
总窗口包含服务端指令和历史；独立最大输入、最大输出仍未核实，也未完成窗口极限测试。

## Responses、多轮回放与压缩

```bash
curl http://127.0.0.1:18789/v1/responses \
  -H "Authorization: Bearer sk-your-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "person-deepseek-v4-1-flash-261017",
    "input": "只回复 OK",
    "reasoning": {"effort": "low"},
    "store": false
  }'
```

支持 `input_text`、`input_image`、函数调用与结果、可见思考项和流式事件。
`stream: true` 返回有序的增量事件及 `response.completed / incomplete / failed` 终态。
Chat 与 Responses 使用同一套图片上传、工具转换、权限和 Key 并发控制。

默认 `store: true` 把响应和历史缓存在当前进程内存，供 `previous_response_id`、
`GET /v1/responses/{id}`、`DELETE /v1/responses/{id}` 和
`GET /v1/responses/{id}/input_items` 使用。缓存有效期 1 小时，最多 128 条、
合计 64 MiB；响应过大时返回 `store: false`。缓存会因到期、容量淘汰或重启失效。
`store: false` 不缓存响应正文，调用方可自行回传历史。

`POST /v1/responses/compact` 接受 `model` 与 `input`，调用同模型生成摘要，
保留系统消息和最近完整轮次，返回加密 `compaction` 项；下一次把其 `output`
放进 Responses `input` 即可继续。摘要会消耗上游额度，也可能丢失细节。
这由网关生成摘要实现，不是豆包原生压缩或自动后台压缩。

`include: ["reasoning.encrypted_content"]` 支持网关签发的思考回放。
思考和压缩令牌绑定当前 API Key 的 ID、权限与模型，24 小时后过期，不能跨 Key、
跨模型或跨网关密钥使用。仅加密密钥保存于本地 `data/response-replay.key`，
响应正文不会随缓存写入磁盘；请保护此文件，删除后旧令牌无法解密。

不提供 Anthropic Messages、生图、生视频、后台 Responses 或内置网页搜索工具。
这些能力及不支持的参数会明确报错。

为兼容客户端默认参数，Chat Completions 和 Responses 统一接收但忽略以下附加项，
不把它们传给上游，也不据此宣称已实现对应功能：

| 附加项 | 处理方式 |
| --- | --- |
| `temperature`、`top_p`、`frequency_penalty`、`presence_penalty`、`seed`、`logit_bias` | 使用豆包官方默认采样，不控制温度、惩罚、随机种子或 Token 偏置 |
| `logprobs`、`top_logprobs` | 不额外返回或伪造概率数据 |
| `service_tier`、`safety_identifier` | 不切换服务档位，不上传此客户端标识 |
| `prompt_cache_key`、`prompt_cache_retention`、`prompt_cache_options` | 不控制官方缓存或缓存保留时间 |
| `stream_options.include_obfuscation`、`stream_options.continuous_usage_stats` | 不增加混淆填充或连续用量事件；Chat 的 `include_usage` 保持原功能，Responses 保持现有用量输出 |
| Responses 的 `reasoning.summary`、`reasoning.generate_summary` | 不额外生成或控制摘要，实际返回的思考内容保留原转换 |
| Responses 的可选 `include`：概率、输入图片 URL、搜索来源/结果、代码执行输出、计算机工具图片 URL | 忽略无法提供的附加数据，不启用相应工具、不伪造结果；`reasoning.encrypted_content` 保持原功能 |

`n: 1` 可以正常请求，`n > 1` 仍报错，不会静默减少候选数量；Responses 接收默认的
`modalities: ["text"]`，其他输出模态仍不支持。`reasoning.effort`、工具与工具选择、
图片输入、JSON Schema、输出长度、停止序列及上下文回放继续按原逻辑校验和执行。
Responses 的后台执行、服务端会话等未实现功能，以及未知的 Responses 字段仍明确报错。

## JSON 输出与长度控制

Chat 使用 `response_format`，Responses 使用 `text.format`，支持 `text`、
`json_object` 与 `json_schema`。网关先给模型格式提示，再实际解析并验证输出；
不合格时返回错误，不能保证上游每次遵循 Schema。Schema 仅允许本地引用，
不下载远程或本机文件引用；`strict: true` 的工具参数也会验证。
JSON 和工具响应先缓冲校验，普通文本实时增量发送。

`max_tokens / max_completion_tokens`（Chat）、`max_output_tokens`（Responses）
实现网关侧保守输出预算，正文与可见思考共用预算；达到预算返回 `length`
或 `incomplete`。Chat 支持跨数据块匹配 `stop`。普通流达到预算或 stop 会关闭
上游连接；结构化输出需先收完整答案再校验，因此不能据此保证精确控制上游计费。

`truncation` 默认 `disabled`，估算超窗明确报错；设为 `auto` 时，仅裁掉最旧的
完整用户轮次，保留系统指令、最近轮次及其工具调用与结果，仍超窗则报错。
网关为服务端指令预留 8,192，并为输出预留请求预算（未指定时为 4,096）。

`usage` 和窗口预算以 UTF-8 字节数保守估算文本，每张图片另预留 4,096；
带有估算标记，不是官方 tokenizer 的实测值，可能比实际 token 数更保守，不能用于精确计费。

## 图片输入

在用户消息中使用 OpenAI `image_url` 内容块，可与文字混合，也可只发送图片：

```json
{
  "model": "person-deepseek-v4-1-flash-261017",
  "messages": [{
    "role": "user",
    "content": [
      {"type": "text", "text": "描述这张图片"},
      {"type": "image_url", "image_url": {"url": "data:image/png;base64,<图片Base64>"}}
    ]
  }]
}
```

`url` 也可使用公网 HTTP/HTTPS 图片地址。
支持 PNG、JPEG、WebP、GIF，每次最多 **8 张**，每张最多 **10 MiB**，总计最多
**20 MiB**，以上限制包含历史消息中的图片。下载 URL 只允许公网主机的 80/443 端口，
不接受登录凭证、内网地址和指向内网的重定向；无法下载的图片可改用 Base64。

网关会把图片上传到豆包官方存储并注册为当前账号的图片资源，再随聊天请求发送。
图片仅在请求内存中处理，不在网关落盘，上传授权和图片 URI 不在账号间共享。
公网图片下载连接独立于账号连接，不发送账号 Cookie 或网关 API Key。

现有 8 个模型均通过网关图片通道的基础合成图识别测试，模型清单宣告
`image_input: true`。2026-11-13 对其余 7 个模型顺序实测，全部正确识别颜色和形状，
其中 6 个也读对随机数字；Kimi K3 将像素数字误判为验证码而拒读。
这验证了当前通道的图片输入与基础识别，不代表所有图片任务均能成功，
也不等同于确认各底层模型原生视觉架构；具体表现仍取决于上游。
`image_url.detail` 可传 `auto / low / high`，目前均采用上游默认处理，不对应精确的
OpenAI 图片 token 计价。响应 `usage` 中的图片部分仅为固定预算估算。

## 配置

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DOUBAO_WORK_HOST` | `127.0.0.1` | 监听地址 |
| `DOUBAO_WORK_PORT` | `18789` | 监听端口 |
| `DOUBAO_WORK_API_KEY` | `sk-local-change-me` | 网关 API Key |
| `DOUBAO_WORK_ADMIN_PASSWORD` | `admin` | 首次初始化管理密码，与 API Key 独立 |
| `DOUBAO_WORK_ACCOUNTS_DIR` | `accounts` | 账号 JSON 目录 |
| `DOUBAO_WORK_DATA_DIR` | `data` | 工作区等运行时数据 |
| `DOUBAO_WORK_TIMEOUT` | `180` | 单次上游请求超时秒数 |
| `DOUBAO_WORK_COOLDOWN_SECONDS` | `60` | 限流/故障冷却秒数 |

设置保存在 `data/settings.json`。首次初始化时环境变量中的 API Key 会导入为
默认 Key；后续以持久化设置为准，停用或删除后不会因重启恢复。
默认模型、超时、冷却在面板保存后生效并在重启后保留；有上游请求运行时先等待完成再保存。
「设置 → 运行配置 → 账号级并发上限」支持 `0～10` 的整数：`0` 为默认值，
网关不限制每账号并发；`1～10` 表示每账号同时最多接对应数量的请求。
该设置与每 API Key 并发限制分别生效，旧配置自动补为 `0`。
仅修改账号并发上限时可在请求运行中保存，不中断现有请求；降低上限后等现有请求
自然结束再接新请求。达到上限时尝试其他允许使用的账号，全部账号忙碌返回
`429 / account_concurrency_limit`，不排队；冷却提示单独显示并向上取整到秒。
上游仍可能自行限流，`0` 不代表官方承诺无限并发。
端口、目录和上游地址仍由环境变量配置并在重启后生效。

管理会话仅用于 `/admin/*`，模型调用需单独使用 API Key。
导入导出、管理设置和 API Key 限制适用于单进程部署；多实例不要共用可写账号/设置目录。
