# 第三方来源与致谢

## yyydsz/doubao2api

- 项目：https://github.com/yyydsz/doubao2api
- 参考版本：`b867f76a9a2db43c6d291bbcebf6c02cc1e3c024`
- 许可：MIT，完整原文见 [licenses/yyydsz-doubao2api-LICENSE.txt](licenses/yyydsz-doubao2api-LICENSE.txt)。
- 本项目参考其工作模式网关设计，并将 Passport 扫码接口参数、扫码状态映射等协议适配为 Python 实现；同时参考了额度窗口解析、会话粘滞及 SSE 心跳设计。
- 没有捆绑或要求运行其 Go 网关或 Python 管理面板。
- 客户端参数兼容策略参考其“只提取实际使用字段”的设计，以共享参数表独立实现；不会将接受但忽略的参数声明为上游已支持。
- 新增原生会话复用、大请求窗口预算及用量日志持久化，参考其功能思路独立实现，并增加账号/Key/模型/凭证/精确历史隔离。

## 3294798852/doubao-work-gateway

- 项目：https://github.com/3294798852/doubao-work-gateway
- 参考版本：`79b97ad37a401de8804f6b51bcc69657dcc6ae0a`
- 本项目参考其功能设计和公开协议研究，包括官方模型菜单、网页设备上下文、额度窗口分别展示、每日任务发现以及“提交前记录、领取后回查”的交互。
- 该仓库 `LICENSE` 明确将 MIT 授权限定在作者的新增贡献；继承基线没有仓库级统一复用许可。本项目没有复制其继承网关源码、界面、品牌图标或截图，上述功能由本项目独立编写为 Python 实现。
- 参考不表示与这些项目或豆包官方存在隶属、合作或担保关系。
- 同样参考其请求字段筛选与未实现功能保留校验的方式，独立实现 Chat / Responses 的附加参数兼容；未复制其请求解析源码。
- 新增消息/块级快照去重、可选标准用量透传、独立浏览器登录、账号验证状态和加密存储，参考其设计由本项目独立编写；不承诺上游每次提供 Token 或积分账单。

## 运行依赖

FastAPI、Starlette、httpx、h2、Pydantic、Uvicorn、cryptography、jsonschema 和 qrcode 分别遵循各自随包分发的许可证。项目截图、账号导出、Cookie 和运行数据不作为发行资源。
