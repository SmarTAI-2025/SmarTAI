# 百度文档解析（Unlimited-OCR）BYOK 契约核验

> 查询日期：2026-08-23（Asia/Singapore）
> 证据范围：只使用百度官方产品、API、计费、认证与开源仓库资料。登录后账号资源状态尚未核验。

## 1. 准确名称与产品边界

| 层级 | 官方名称 | 本项目结论 |
| --- | --- | --- |
| 开源模型 | `Unlimited-OCR` / `baidu/Unlimited-OCR` | 可自托管的开源模型；不是百度托管 API 的 `model` 参数，也不代表云 API 无限调用。 |
| 百度托管能力 | **文档解析（Unlimited-OCR）** | 文字识别 OCR / 智能文档分析平台下的异步文档解析 API。请求没有 `model` 字段。 |
| API 路径标识 | `unlimited-ocr-parser` | 固定出现在两个官方 endpoint 中，不是通用 LLM provider 或公共模型池条目。 |

“Unlimited”是模型/产品名称及长文档能力表述。托管 API 仍有单文件、页数、QPS 和账号免费页数限制，不能宣传为“不限页”“不限量”或“永久免费”。

## 2. 2026-08-23 可核验的计费事实

结论是：**公测阶段的限时免费测试资源，不是永久免费、不是无限免费、也没有公开可购买的 Unlimited-OCR SKU。**

- 专用 API 文档写“接口限时免费”：个人实名 200 页、企业实名 1,000 页。
- 官方《免费测试资源》表进一步写明：`文档解析（Unlimited-OCR）` 处于“公测阶段，暂不支持开通付费，可申请调额”。
- 同页总则写明成功调用和失败调用都会消耗免费测试资源；官方没有解释失败的多页任务如何扣页。
- Unlimited-OCR 这一行没有标注 `/月`。官方没有公布额度重置周期、有效期、续发规则、免费期截止日、调额条件或最大调额值。
- 官方《产品价格》正文只给普通“文档解析”和“文档解析（PaddleOCR-VL）”定价，没有 Unlimited-OCR。因此其中 0.09 元/页、资源包价格和活动价均不得套用。

所以截至查询日，可写的实际价格只有：**账号获发的免费测试页额度内不产生公开购买价；额度外暂不能公开付费开通，只能申请调额。** 未来商用价格必须以届时登录控制台的订单页、百度书面报价或合同为准。

## 3. 开通、认证与地区边界

前置条件：

1. 注册百度智能云账号并完成个人或企业实名认证。
2. 进入文字识别控制台，等待免费测试资源发放；官方称通常约 10 分钟，可在“资源列表”查看。
3. 创建 OCR 应用，并在服务接口列表勾选该能力的调用权限。
4. 取得该应用的 `API Key` 与 `Secret Key`。

认证流程：

```text
POST https://aip.baidubce.com/oauth/2.0/token
  grant_type=client_credentials
  client_id=<API Key>
  client_secret=<Secret Key>

POST .../unlimited-ocr-parser/task?access_token=<derived token>
POST .../unlimited-ocr-parser/task/query?access_token=<derived token>
```

官方通用认证文档称 access token 有效期为 30 天。API Key、Secret Key 和派生 token 都不得共享、硬编码或写入普通日志。

公开 API 只给固定主机 `aip.baidubce.com`，没有 region 参数；官方公开资料没有承诺物理处理地区、数据驻留地区、境外账号可用性或区域 SLA。不能从中文控制台或人民币定价页面自行推断地区事实。如地区/驻留是上线条件，必须取得百度工单或合同确认。

## 4. 官方 API 契约

### 4.1 提交任务

```http
POST https://aip.baidubce.com/rest/2.0/brain/online/v2/unlimited-ocr-parser/task?access_token=...
Content-Type: application/x-www-form-urlencoded
```

Body：

- `file_data` 与 `file_url` 二选一；`file_data` 是原始文件的纯 Base64，不带 data URI 前缀。
- `file_name` 必填且后缀必须正确。
- 本 adapter 首版**只接受本地文件字节并发送 `file_data`**，不开放 `file_url`。官方没有定义 URL scheme、重定向、私网地址、DNS、鉴权 URL 或 SSRF 安全合同。

官方列出的 `file_data` 格式：

- 版式文档：`pdf`、`jpg`、`jpeg`、`png`、`bmp`、`tif`、`tiff`、`ofd`；
- 流式文档：`doc`、`docx`、`txt`、`wps`、`ppt`、`pptx`。

官方限制：

- 图片不超过 10M，最长边不超过 8192 px；
- 版式文档不超过 100M；流式文档不超过 50M；
- PDF 最多 500 页；
- 紧接上述说明又写“文档大小超过 50M，须从 `file_url` 上传”。因此 50–100M 版式文档的 Base64 合同存在歧义；官方也没有定义 `M` 是十进制 MB 还是 MiB。首版 `file_data` 保守限制为 50,000,000 原始字节，图片限制为 10,000,000 原始字节。

成功响应的稳定必要字段是 `result.task_id`。官方没有幂等键，也没有说明提交超时后的查询/恢复方式；为避免重复扣页，**提交请求不自动重放**。

### 4.2 查询任务与结果

```http
POST https://aip.baidubce.com/rest/2.0/brain/online/v2/unlimited-ocr-parser/task/query?access_token=...
Content-Type: application/x-www-form-urlencoded

task_id=<submit response task_id>
```

- 状态：`pending`、`running`、`success`、`failed`。
- 官方建议提交后每 5–10 秒轮询。
- 提交接口 QPS 2，查询接口 QPS 5；官方没有说明统计主体、突发窗口或 `Retry-After` 合同。
- 成功结果包含 `markdown_url` 与 `parse_result_url`，文档称链接有效期 30 天；但示例把 JSON 链接标成“后续支持”，也没有公开 JSON schema。因此首版只消费 Markdown。
- 链接有效 30 天不等于百度承诺 30 天后删除数据；公开资料没有数据删除/保留合同。

## 5. 安全实现边界

本次 adapter 固定遵守：

- 仅保存每位 owner 自带的 `API Key + Secret Key`，分别用 AES-GCM 加密并以 owner/record/field 绑定 AAD；派生 token 只在进程内短暂缓存。
- 独立 OCR credential repository/API/factory；不复用 `ProviderConfig`、`ExpertRegistry`、通用评分 provider、共享模型池或默认/vision selector。
- factory 必须显式接收 `(owner_id, credential_id)`；错 owner、缺失或解密失败都 fail closed，绝不 fallback。
- 固定官方 HTTPS endpoint，不接受自定义 base URL，不读取任何平台级百度密钥环境变量。
- token、query URL、凭据、原始厂商错误、文件内容和 OCR 内容均不进入普通日志或用户错误响应。
- 认证/查询可做有界重试；计费相关的提交请求只发送一次。轮询有总超时，间隔不快于官方建议的 5 秒。
- 当前不提供文档识别 API 或默认任务路由，也未实现跨请求的 owner/application 级并发与 QPS 调度。百度未公开 QPS 的统计主体；任何后续路由接入都必须先按最保守口径实现共享限流，不能仅依赖单任务 5 秒轮询。
- 当前不可选择的骨架在 provider 边界执行后缀与保守字节上限；Tool 自身尚未校验 8192 px 图片边长和 500 页 PDF，只能由调用方已有上传边界（若覆盖）及百度拒绝兜底。真正接入任务路由前，需要把这两项放入可终止的本地媒体检查边界，不能在 Web 进程里直接解析不可信文件。
- 本切片不接任务默认路由、不增加共享 token、不开放公共模型项，也不加入公网友好宣传。

## 6. 尚未完成的 live smoke

自动测试只验证固定 HTTP 契约、错误投影、超时/重试、日志脱敏、加密和 owner 隔离，不调用百度或消耗页数。

Live smoke 仍需要：

1. Annie 自有、已完成实名认证且已勾选“文档解析（Unlimited-OCR）”权限的 OCR 应用；
2. 通过 SmarTAI 安全 BYOK 入口录入 API Key 与 Secret Key（不要在聊天、截图或日志中发送）；
3. 一份合成或去标识的代表性图片/PDF；
4. 控制台“资源列表”中该能力一行的非秘密截图，用于核验认证类型、已发放/剩余页数、生效/失效日、是否刷新及当前服务状态。

Token 换取成功只能证明凭据有效，不能证明该应用已获接口权限、账号仍有额度、结果下载主机满足本地安全策略或真实文档解析成功。

## 7. 百度官方来源

全部链接访问/复核日期：2026-08-23。

- [文档解析（Unlimited-OCR）API 文档](https://ai.baidu.com/ai-doc/OCR/fmr1p39gb)（更新 2026-07-30）
- [免费测试资源 - 文字识别 OCR](https://cloud.baidu.com/doc/OCR/s/fk3h7xu7h)（更新 2026-07-22）
- [功能发布记录](https://ai.baidu.com/ai-doc/OCR/8m1gdhfkv)（更新 2026-07-22；记录 2026-07-02 开放公测）
- [新手操作指引](https://cloud.baidu.com/doc/OCR/s/dk3iqnq51)（更新 2026-06-05）
- [鉴权认证机制](https://cloud.baidu.com/doc/AI_REFERENCE/s/um3zhy50e)（更新 2025-01-20）
- [错误码 - 文字识别 OCR](https://cloud.baidu.com/doc/OCR/s/wlqc0cfdq)（更新 2025-02-25）
- [产品价格 - 文字识别 OCR](https://cloud.baidu.com/doc/OCR/s/tlrzzplc1)（更新 2026-06-10；正文没有 Unlimited-OCR 价格）
- [文档解析产品页](https://cloud.baidu.com/product/OCR/doc_parser.html?from=cloud_banner)
- [百度官方 Unlimited-OCR 开源仓库](https://github.com/baidu/Unlimited-OCR)
- [Unlimited-OCR 企业级服务公告](https://cloud.baidu.com/support/news?action=detail&id=3274)（发布 2026-07-06）
