# SmarTAI 邮箱账号流程后端实现交接

> 决策日期：2026-08-24
> 状态：实现合同已冻结；本地认证定向测试、前端定向测试、类型检查和构建已完成。PostgreSQL 与真实投递仍待具备外部环境；生产联调和仓库既有全量失败不属于本轮认证改动
> PR 拆分：PR A「邮箱验证注册」→ 合并后 PR B「忘记密码与重置」
> 范围：两个独立小 PR；不与 #35/#37 或其他账号扩展混合
> 配置与前端清单：`GMAIL_SMTP_CONFIGURATION_AND_FRONTEND_INTEGRATION_CN.md`

> 2026-08-30 集成说明：原定的 A→B 顺序仍用于依赖判断，但本候选已经在最新 `main` 上一次性完成四段集成，并将迁移线性顺延为 `0013_email_verification_requests → 0014_password_reset_requests`。当前不存在共享、部署或真实用户数据库，因此无需保留未合并旧 revision 的本地迁移身份。

本文只描述后端实现合同和自动测试。人工体验验证方案等两个 PR 形成稳定候选后再单独制定。

## 一、冻结后的账号范围

### PR A：邮箱验证注册

- 学校邮箱填写注册信息后自动收到 30 分钟一次性验证链接。
- 打开邮件只进入确认页，确认后才创建普通教师账号。
- 验证前不创建正式用户、session、文件空间、BYOK、资料库或模型额度。
- 验证完成后不自动登录，返回登录页正常登录。
- 不要求管理员预先登记邮箱，也不要求输入注册邀请码。
- 注册资格只由可配置学校邮箱域名控制。

### PR B：忘记密码与重置

- 登录页提供忘记密码入口。
- 已注册邮箱可以请求 30 分钟一次性密码重置链接。
- 提交新密码时才消费链接；完成后不自动登录。
- 重置成功必须撤销该账号的现有登录会话，旧密码失效，新密码生效。

### 与免费共享 API 的边界

注册邀请码不属于本次账号流程。未来若开放平台共享 API，再由已经完成邮箱验证的账号输入正确邀请码兑换额度；邀请码不能参与注册、改变角色或绕过邮箱域名限制。

## 二、邮箱域名规则

首期配置：

```text
SMARTAI_ALLOWED_EMAIL_DOMAINS=ustc.edu.cn
```

每一项表示一个基础域名，允许该域名本身及以 `.` 为边界的全部子域名：

| 邮箱 | 结果 | 原因 |
| --- | --- | --- |
| `name@ustc.edu.cn` | 允许 | 精确基础域名 |
| `name@mail.ustc.edu.cn` | 允许 | `ustc.edu.cn` 的子域名 |
| `name@mails.ustc.edu.cn` | 允许 | `ustc.edu.cn` 的子域名 |
| `name@dept.mail.ustc.edu.cn` | 允许 | 更深层子域名仍在边界内 |
| `name@evilustc.edu.cn` | 拒绝 | 缺少点边界，不是子域名 |
| `name@ustc.edu.cn.example.com` | 拒绝 | 实际归属 `example.com` |
| `name@gmail.com` | 拒绝 | 未在允许列表 |

实现要求：

- 提取 `@` 后域名，trim、转小写并移除结尾的单个 `.` 后比较。
- 匹配条件只能是 `domain == allowed` 或 `domain.endswith("." + allowed)`。
- 多所学校使用逗号分隔，例如 `ustc.edu.cn,tsinghua.edu.cn`。
- 显式配置 `*` 表示不限制邮箱域名。
- production 未设置或配置为空时 fail closed，不能因漏配意外开放全部邮箱。
- 请求注册和最终验证时都重新检查，不能只依赖前端提示。
- 邮箱是否真实存在由验证邮件证明，不额外维护受邀邮箱表。

## 三、共享 SMTP transport

PR A 先建立可复用邮件发送层，PR B 直接复用，不再实现第二套发信代码。

业务 service 只依赖可注入接口，例如：

```text
EmailSender.send(
  to_email,
  subject,
  text_body,
  html_body
)
```

真实环境注入 SMTP sender，自动测试注入 fake sender。业务代码不得判断 Gmail 或 126，也不得写死 host、端口、账号或 TLS 模式。

建议配置合同：

```text
SMARTAI_SMTP_HOST
SMARTAI_SMTP_PORT
SMARTAI_SMTP_SECURITY        # starttls 或 ssl
SMARTAI_SMTP_USERNAME
SMARTAI_SMTP_PASSWORD        # 应用专用密码／客户端授权码
SMARTAI_MAIL_FROM_ADDRESS
SMARTAI_MAIL_FROM_NAME
SMARTAI_PUBLIC_FRONTEND_URL  # 生成两类邮件链接的唯一可信 HTTPS origin
SMARTAI_ALLOWED_EMAIL_DOMAINS
SMARTAI_SMTP_TIMEOUT_SECONDS
```

首期 Gmail 值：

```text
SMARTAI_SMTP_HOST=smtp.gmail.com
SMARTAI_SMTP_PORT=587
SMARTAI_SMTP_SECURITY=starttls
SMARTAI_SMTP_USERNAME=smartai.univ@gmail.com
SMARTAI_MAIL_FROM_ADDRESS=smartai.univ@gmail.com
SMARTAI_MAIL_FROM_NAME=SmarTAI
SMARTAI_ALLOWED_EMAIL_DOMAINS=ustc.edu.cn
```

本地联调时，`SMARTAI_SMTP_PASSWORD` 只从仓库根目录 `.env` 读取。`SMARTAI_PUBLIC_FRONTEND_URL` 必须是预先配置的 origin，禁止依据请求 `Host` header 拼接验证或重置链接。

配置落点必须保持一致：

- 本地开发：写入仓库根目录 `.env`。当前 `backend/config.py` 的 `env_file=".env"` 按后端启动工作目录读取，因此后端从仓库根目录启动；不创建 `backend/.env`。
- 配置模板：PR A 在仓库根目录 `.env.example` 增加全部变量名和非敏感示例值，`SMARTAI_SMTP_PASSWORD` 只留空或写占位符，不能填真实授权码。
- 前端：不得写入 `frontend/app/.env*`、Cloudflare Pages 变量或任何浏览器可见配置。

本地已有 `.env` 时只追加或修改所需键，不能用 `.env.example` 覆盖现有文件。`.env` 已由 `.gitignore` 排除。

SMTP 要求：

- `starttls` 使用普通 SMTP 连接后升级 TLS；`ssl` 使用直接 TLS 连接。
- TLS 证书必须校验，连接和读取必须有有限超时。
- `From` 默认与认证邮箱一致。
- production 缺少 SMTP 必要配置时 fail closed，不得返回“邮件已发送”。
- 普通日志只记 request/trace ID、投递阶段和稳定错误码；不记录密码、token、邮件正文、SMTP secret 或供应商原始认证错误。
- CI 不连接真实 Gmail，不保存 Gmail secret。

Gmail／126 切换项必须全部从上述环境变量读取，不增加 `if gmail`／`if 126` 等供应商业务分支，也不需要额外的 `SMARTAI_SMTP_PROVIDER`。以后切换 126 时只修改后端部署环境中的 `SMARTAI_SMTP_HOST`、`SMARTAI_SMTP_PORT`、`SMARTAI_SMTP_SECURITY`、`SMARTAI_SMTP_USERNAME`、`SMARTAI_SMTP_PASSWORD`、`SMARTAI_MAIL_FROM_ADDRESS` 和 `SMARTAI_MAIL_FROM_NAME`，重启后端并重新做真实投递 smoke；注册/重置 service、数据库、API、前端代码和构建产物均不改变。

## 四、PR A：邮箱验证注册

### 4.1 `POST /auth/register/request`

请求：

```json
{
  "username": "teacher_demo",
  "email": "teacher@mail.ustc.edu.cn",
  "password": "至少8位密码"
}
```

请求体不得接受 `role`、`invite_code`、admin 标记或模型额度字段。

后端顺序：

1. 用户名 trim，邮箱 trim + lowercase；用户名 3～64 字符，密码 8～128 字符。
2. 按基础域名规则检查邮箱。
3. 检查当前没有可用正式账号；对外不得通过不同文案泄露邮箱或用户名是否已存在。
4. 安全哈希密码，只保存密码哈希。
5. 生成至少 128 位 CSPRNG 随机 token，只保存 token 摘要。
6. 创建待验证记录，生成 `/register/verify#token=...` 链接。
7. 通过共享 SMTP sender 发送邮件。
8. SMTP 接受邮件后返回 `verification_required`；投递失败返回安全错误，不创建正式账号。

成功建议返回 `202`：

```json
{
  "status": "verification_required",
  "request_id": "opaque-request-id",
  "expires_in_seconds": 1800,
  "resend_after_seconds": 60
}
```

稳定失败分类至少包括：

- `registration_email_domain_not_allowed`
- `registration_rate_limited`
- `registration_email_delivery_failed`
- `registration_unavailable`

### 4.2 `POST /auth/register/resend`

请求：

```json
{"request_id":"opaque-request-id"}
```

要求：

- 冷却至少 60 秒，并限制同一邮箱和来源 IP 的发送频率。
- 再次检查待验证流程和邮箱域名。
- 生成新 token；新邮件发出后旧 token 失效。
- 返回新的 `request_id`、1800 秒有效期和 60 秒冷却。
- SMTP 失败不创建账号，不泄露底层异常，可安全重试。

### 4.3 `POST /auth/register/verify`

请求：

```json
{"token":"high-entropy-one-time-token"}
```

成功：

```json
{"status":"registered"}
```

同一 token 已成功完成注册后再次确认：

```json
{"status":"already_verified"}
```

错误码：

- `verification_link_expired`
- `verification_link_already_used`
- `verification_link_invalid`
- `registration_rate_limited`
- `registration_unavailable`

最终验证必须再次检查邮箱域名、用户名和邮箱唯一性。创建普通 `teacher`、标记待验证记录完成必须在同一数据库事务中执行。

### 4.4 待验证记录

至少包含：

```text
id / request_id
normalized_username
normalized_email
password_hash
token_digest
created_at / expires_at / resend_available_at
superseded_at / verified_at
delivery_status / last_delivery_error_code
```

要求：

- 不保存明文密码或明文 token。
- 同一 token 摘要唯一；同一流程只有一个当前有效 token。
- 同一邮箱同时提交多次时，只保留一个最新有效流程。
- 重发原子废弃旧 token。
- 并发消费最多创建一个账号；第二个请求返回 `already_verified` 或稳定已使用结果。
- `User.username`、`User.email` 的数据库唯一约束保留为最后防线。
- 过期记录可定期或在请求时顺手清理，不为首轮增加独立复杂队列。

### 4.5 邮件内容

- 主题：`确认 SmarTAI 教师账号`
- 说明链接 30 分钟有效；非本人操作可忽略。
- 同时提供按钮和纯文本完整链接。
- 打开链接后仍需点击确认；邮件扫描器打开页面不能自动建号。
- 不包含密码、学生信息、模型额度、营销内容或追踪像素。

### 4.6 旧 `/auth/register`

现有匿名 `/auth/register` 不能继续直接创建用户、access token 和 refresh session。PR A 接通后取消公开挂载或在 production 稳定拒绝。开发播种和内部建号改用受控 CLI/工具，不保留匿名 HTTP 绕过路径。

### 4.7 PR A 自动测试

- 完整成功：request → verify → login。
- `ustc.edu.cn`、`mail.ustc.edu.cn`、`mails.ustc.edu.cn` 允许。
- `evilustc.edu.cn`、外校域名和伪后缀拒绝。
- 未配置域名时 production fail closed；显式 `*` 才开放全部域名。
- 密码少于 8 位拒绝。
- 验证前正式 `User` 不存在且不能登录。
- 每个申请 token 不同；A token 不能验证 B 申请。
- 过期、损坏、重复确认和并发双击。
- resend 后旧链接失效。
- SMTP 失败/超时不创建账号、不返回假成功。
- 请求体夹带 `role=admin`、`invite_code` 或其他字段不能创建管理员。
- 旧 `/auth/register` 不能绕过邮箱验证。
- 数据库、日志和 response 不含明文密码、token 或 SMTP secret。
- migration upgrade、SQLite/PostgreSQL 现有检查保持单 head 和全绿。

## 五、PR B：忘记密码与重置

PR B 必须在 PR A 合并后从最新 `main` 创建，直接复用 SMTP transport、固定 frontend origin、token 生成/摘要工具和邮件安全边界。

### 5.1 `POST /auth/password-reset/request`

请求：

```json
{"email":"teacher@mail.ustc.edu.cn"}
```

统一返回中性结果，不能让匿名调用者判断账号是否存在：

```json
{
  "status": "reset_link_requested",
  "expires_in_seconds": 1800,
  "resend_after_seconds": 60
}
```

若邮箱对应有效账号，生成 `/reset-password#token=...` 并发送；不存在、停用或不符合条件时仍返回同样外观，不发送邮件。内部只记录安全分类。

### 5.2 `POST /auth/password-reset/confirm`

请求：

```json
{
  "token": "high-entropy-one-time-token",
  "new_password": "新的至少8位密码"
}
```

成功：

```json
{"status":"password_reset"}
```

要求：

- token 至少 128 位随机量、只存摘要、30 分钟有效、只能成功一次。
- 同一账号新请求发出后，旧重置链接失效。
- 新密码 8～128 字符并安全哈希。
- 更新密码、消费 token、撤销该用户全部 refresh session 在同一事务中完成。
- 当前 JWT access token 最长可存活 30 分钟；PR B 必须加入最小的 session invalidation 标记（例如 `auth_invalid_before` 或等价版本字段），使密码重置前签发的 access token 立即失效，不能只等待自然过期。
- 成功后不签发新 token、不自动登录。

稳定错误码：

- `password_reset_link_expired`
- `password_reset_link_already_used`
- `password_reset_link_invalid`
- `password_reset_rate_limited`
- `password_reset_unavailable`

### 5.3 密码重置记录

建议独立于待验证注册记录，避免两种业务状态互相污染：

```text
id
user_id
token_digest
created_at / expires_at / resend_available_at
superseded_at / consumed_at
delivery_status / last_delivery_error_code
```

共享 token 工具和 SMTP sender，但不把注册密码哈希、用户名或注册状态放入重置记录。

### 5.4 邮件内容

- 主题：`SmarTAI 密码重置`
- 说明链接 30 分钟有效；非本人操作可忽略并保留原密码。
- 同时提供按钮和纯文本链接。
- 打开页面不会自动改密码；提交新密码时才消费 token。
- 不包含旧密码、新密码、学生信息或追踪像素。

### 5.5 PR B 自动测试

- 已存在邮箱和不存在邮箱得到相同公开响应。
- 合法链接可以设置新密码；旧密码失败，新密码成功。
- 过期、损坏、重复和被新请求替代的旧链接失败。
- 并发 confirm 最多成功一次。
- 重置成功撤销全部 refresh session。
- 重置前签发的 access token 立即失效，而不是继续有效 30 分钟。
- SMTP 失败不修改密码、不消费可重试状态、不泄露账号存在性。
- 日志、数据库和 response 不含明文新密码、token 或 SMTP secret。
- 成功后不自动登录，也不返回 access/refresh token。

## 六、频率限制

两类邮件共用统一的限流基础，分别计数：

- 同一流程重发至少间隔 60 秒。
- 同一邮箱默认最多 5 封/小时。
- 同一来源 IP 默认最多 20 封/小时。
- 阈值全部可配置；达到后返回稳定错误码和 `Retry-After`。
- 注册只能给允许域名发送；重置只对现有有效账号实际发送。
- 双击、网络重放和并发请求不能造成邮件风暴。
- PostgreSQL 的邮箱/IP 计数检查与新记录写入必须处于同一事务级 advisory lock 内，request 与 resend 使用相同的邮箱/IP 锁键，避免多 worker 绕过阈值。

## 七、迁移与合并顺序

```text
最新 main
  → PR A：通用 SMTP + 邮箱验证注册 + 旧入口封堵
  → PR A 合并
  → 最新 main
  → PR B：忘记密码 + 全会话失效
  → PR B 合并
```

- 两个 PR 各自使用当时最新的顺延迁移编号，禁止预先写死 revision 序号。
- PR B 不堆叠在未合并 PR A 上长期等待；PR A 合并后再建立 PR B，减少反复改 base。
- PR A 不顺手加入忘记密码页面或 session invalidation。
- PR B 不顺手加入更换邮箱、修改用户名、双因素登录或管理员账号管理。

## 八、Ready 与完成回报

每个 PR 转 Ready 前至少提供：

```text
PR：
exact head：
base/main SHA：
迁移 revision：
改动文件范围：
定向测试：
SQLite/PostgreSQL/完整 CI：
接口 curl 示例：
旧入口或旧 session 的最终处置：
需要配置的环境变量名称：
仍未验证的真实环境项：
```

回报不得包含 Gmail 应用专用密码、完整 token、真实账号密码或真实用户邮箱列表。

## 九、非本轮范围

- 邮件验证码和短信验证码。
- 管理员预登记邮箱和注册邀请码。
- 免费共享 API 邀请码/额度账本。
- 修改邮箱、修改用户名、账号合并和删除账号。
- 双因素登录和第三方 OAuth 登录。
- 营销邮件、订阅、群发和打开/点击追踪。
- 独立邮件队列、Resend、SES 或多供应商自动 fallback。
- 人工体验验收脚本；等 PR A、PR B 形成稳定候选后另行制定。
