# Gmail SMTP 配置与前端联调清单

本文是邮箱验证注册和密码重置上线前的操作清单。仓库不保存 Gmail 应用专用密码、真实收件地址、访问令牌或其他部署密钥。

## 环境变量

后端从仓库根目录 `.env` 读取配置；不要在 `backend/.env` 或前端目录创建副本。Render 生产服务需要配置以下变量：

```text
SMARTAI_SMTP_HOST=smtp.gmail.com
SMARTAI_SMTP_PORT=587
SMARTAI_SMTP_SECURITY=starttls
SMARTAI_SMTP_USERNAME=<部署邮箱账号>
SMARTAI_SMTP_PASSWORD=<Gmail 应用专用密码>
SMARTAI_MAIL_FROM_ADDRESS=<部署邮箱账号>
SMARTAI_MAIL_FROM_NAME=SmarTAI
SMARTAI_ALLOWED_EMAIL_DOMAINS=ustc.edu.cn
SMARTAI_PUBLIC_FRONTEND_URL=<可信 HTTPS 前端 origin>
SMARTAI_SMTP_TIMEOUT_SECONDS=20
```

在 Render 中，账号、密码和部署专属前端 URL 使用 secret/sync-false 配置；不要把它们提交到 YAML、日志或浏览器环境变量。生产环境要求 `SMARTAI_PUBLIC_FRONTEND_URL` 使用 `https`，不能带 query、fragment、用户名或密码。

本地开发可以使用 fake sender，不需要真实 Gmail 投递。若要本地连接 SMTP，使用单独的测试账号和短期应用专用密码，并在测试结束后撤销。

## 注册接口与前端流程

公开注册只有三个接口：

```text
POST /auth/register/request
POST /auth/register/resend
POST /auth/register/verify
```

注册只接受用户名、学校邮箱和密码，不接受邀请码或角色字段。旧的 `POST /auth/register` 不再挂载，返回 `404`。管理员邀请码数据和管理 API 保留给受控内部工具或未来额度功能，不能用来绕过邮箱验证。

前端注册页提交 request 后保存返回的 `request_id`，显示 resend 控件和 60 秒冷却倒计时。重发成功后必须替换为新的 `request_id`；旧邮件链接随即失效。邮件链接打开确认页不会自动建号，用户必须再次点击确认按钮。

## 限流与错误处理

- 同一注册流程重发至少间隔 60 秒。
- 同一邮箱默认每小时最多 10 封。
- 同一来源 IP 默认每小时最多 20 封。
- 达到阈值返回稳定错误 `registration_rate_limited` 和 `Retry-After`。
- SQLite/单进程使用共享邮箱/IP 锁；PostgreSQL 使用事务级 advisory lock，使 request 与 resend 的计数、判定和写入跨 worker 串行化。
- SMTP 失败返回 `registration_email_delivery_failed`，不会创建正式用户，也不会返回假成功；失败状态可以安全重试。

## 可复现验收

本地命令：

```powershell
python -m compileall -q backend
python -m pytest backend/tests/test_email_registration.py -q
python -m pytest backend/tests/test_password_reset.py -q
python -m pytest backend/tests -q
cd frontend/app
npm test
npm run typecheck
npm run build
```

PostgreSQL 集成测试需要可连接的 `SMARTAI_TEST_POSTGRES_URL` 或本机 Docker PostgreSQL。没有数据库服务时应记录为 skipped，并写明连接失败原因；skipped 不能表述为通过。

真实 Gmail smoke test 只在明确提供测试收件地址后执行一次。执行时仅核对邮件是否收到、链接是否指向配置的 HTTPS origin，以及 request/verify/login 流程是否完成；不要打印密码、完整 token、SMTP secret 或真实用户邮箱列表。

## 部署前检查

1. 检查 Render 环境变量名称与 `backend/render.yaml` 一致。
2. 确认生产 frontend URL 是 HTTPS 且没有路径、查询或 fragment 误配。
3. 确认日志只记录 request/trace id、阶段和稳定错误码。
4. 检查 OpenAPI 中只出现 request/resend/verify 三个公开注册接口。
5. 保存 SQLite、PostgreSQL、后端全量测试、前端测试和真实投递 smoke 的实际结果。

## 2026-08-28 本地验收记录

- 认证、origin、Render、旧路由和管理员邀请码定向测试：`66 passed`（认证核心专项由独立复核记录为 `70 passed`）。
- 前端注册、登录和密码重置页面：`7 passed`；`npm run typecheck` 和 `npm run build` 通过，构建只有既有的 500 kB chunk 警告。
- 前端全量（关闭 Node 实验性 Web Storage 后）：仍有 2 个失败文件、4 个失败测试和 6 个未处理 `AbortSignal` 错误；失败集中在 `QuestionPreparationDetailPage`/`AddProblemsPage` 的既有路由断言与 Node/jsdom `AbortSignal` realm，不在本轮认证代码。注册/登录专项为 `4 passed`。
- 后端全量 fresh：`937 passed, 10 skipped, 4 failed`；4 个失败位于 `test_runner_spike.py` 3 项和 `test_submission_source_pipeline_integrity.py` 1 项，不在认证代码。
- PostgreSQL：本机 Docker daemon 未运行，`SMARTAI_TEST_POSTGRES_URL` 未设置，`127.0.0.1:5432` 不可连接；真实 PostgreSQL 测试未执行，不能表述为通过。
- Gmail：没有明确授权的测试收件地址，未发送真实邮件；fake sender 已覆盖 request/resend、SMTP 回滚和并发行为。
