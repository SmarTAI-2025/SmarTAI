# 管理员业务配置

此实现管理注册资格、邮件限流、两类用户存储上限及模型日额度，入口为独立私有管理端 `/admin/business-config`。不修改部署参数或密钥，不开放共享模型池、付费模型或新的计费能力。

## 配置合同

每次业务请求/存储事务直接执行数据库查询，无进程缓存、无 settings 全局修改。单条 UNION 查询同时读取全局与用户行，优先级为：单用户业务配额覆盖 → 全局 DB 覆盖 → settings → 代码默认。请求使用查询时的已提交快照；提交后的后续请求及其他进程立即可见。正在执行的事务不被追溯取消。

| 字段 | 代码默认 | 后台可保存范围 | 空/零的含义 |
| --- | --- | --- | --- |
| allowed_email_domains | 空字符串 | 最多 64 个根域名、总长 4096；或单独 `*` | `""` 全拒绝；`*` 全允许；null 删除覆盖并继承 |
| email_verification_resend_seconds | 60 | 1–3600 整数秒 | 0、空字符串非法；null 继承 |
| email_verification_hourly_email_limit | 5 | 1–100 整数 | 0、空字符串非法；null 继承 |
| email_verification_hourly_ip_limit | 20 | 1–1000 整数 | 0、空字符串非法；null 继承 |
| unfinished_source_quota_bytes | 536870912 | 0–1099511627776 整数字节 | 0 禁止新增占用；null 继承 |
| knowledge_storage_quota_bytes | 536870912 | 0–1099511627776 整数字节 | 同上 |
| shared_pool_daily_request_limit | 100 | -1–1000000 次 | -1 无限；0 禁止；null 继承 |
| shared_pool_daily_estimated_token_limit | 100000 | -1–1000000000 | -1 无限；0 禁止；null 继承 |
| history_query_llm_daily_limit | 20 | -1–1000000 次 | 同上 |

数字字符串、浮点数、布尔值、未知字段和单用户域名/邮件覆盖均拒绝。原环境变量未设置 DB 覆盖时仍按现有 settings 运行；不把后台新校验追溯应用到旧部署配置。页面显示当前有效值、来源、范围及覆盖状态，恢复继承必须显式使用按钮；空数字输入不会当成 0 或继承。

## 邮箱与发送

- 域名沿用 `email_registration.email_domain_allowed`，复用 IDNA/去首尾空白/大小写规范化。后台配置拒绝 URL、邮箱、端口、IP、单标签、空列表项及混合通配符。`example.edu` 匹配自身与 `.example.edu` 子域，拒绝 `badexample.edu`、`example.edu.evil`。国际化域名存规范 ASCII。
- 当前规则检查 request、resend、首次完成 verify。待验证申请按验证时规则；新规则不修改已建账号，已完成验证的重复请求仍返回 already_verified。收紧后未过期 token 在规则重新允许时仍可验证。
- 尚未在后台管理域名时，找回密码保留原 settings 域名资格语义。首次保存非 null 域名覆盖后，`business_configuration.registration_rules_managed` 置 true 并保留：从此找回密码独立于注册域名，包括后来恢复域名继承。否则在旧规则下合法建立的账号可能永久失去密码找回。所有合法邮箱使用相同匿名限流/中性响应及响应后的账号查询边界；只有存在的活动账号实际发邮件，未知账号不外发。全业务清空会删除标记并恢复部署初始行为。
- 注册与找回密码继续使用各自原表计数（EmailVerificationRequestRecord / PasswordResetRateEventRecord）；设置保存不删除、不清零当前小时计数，不重写既有 token 的 expires_at。
- 新注册邮件使用当前冷却时长；已有注册邮件保留已记录的 resend_available_at，以保持发送失败后的既有恢复语义。密码找回继续按最近匿名请求时间加当前冷却时长检查。保存页面明确说明该区别。
- 管理员协助发送密码重置仍调用相同 request_password_reset，因此同样受当前邮箱/IP/冷却限制；幂等重试不重复发送。管理员账号使用原先的私有找回链接逻辑。
- client IP 沿用既有 Request.client 源，不新增对任意 X-Forwarded-For 的信任。没有新增外发地址、真实 SMTP 或模型调用。

## 存储

两仓库的 `_quota_limit(session, owner_id)` 在现有 usage/reservation 事务查询有效配置，使用真实 quota_owner/knowledge owner。后台用户视图直接复用原 usage 汇总，显示同一个有效上限；未完成任务与知识空间继续独立计量。

保留现有多文件替换、净额预留、去重和释放算法。降低额度不删任何数据、不撤销已持久化预留；已有占用超额时，新预留必须仍满足原有净额校验，删除和释放继续允许，既有知识对象复用不新增字节。不会通过重复提交同一替换组反复取得旧文件抵扣。

## API 与并发

- GET/PATCH `/admin/business-config`：全局配置。
- GET/PATCH `/admin/business-config/users/{user_id}`：该用户存储/模型覆盖与占用；账号不存在返回 404，销户中禁止写入。
- PATCH 必填 `expected_version`、`changes`、非空 `reason`、`Idempotency-Key`。单用户还必填 `expected_global_version`，防止在继承基线已变更后误保存。
- 所有写入使用既有 `administrator_transaction`，在管理锁内重验管理员身份/版本；更新 SQL 还以 version 为条件。冲突返回 409，页面保留修改并要求重新载入，不自动覆盖别人的更新。
- `_audit` 与保存同事务提交，含 actor、target、reason、before/after overrides/version、幂等 response。相同 key/相同 payload 重放原结果；相同 key 不同请求返回 409。网络结果不明时页面保留相同请求的 key 安全重试。
- 全部覆盖清空后保留空行和递增版本，避免版本从 0→修改→删除→0 造成陈旧保存通过；null 只删除对应 JSON key。单用户行随 users 外键 CASCADE 删除，全局行不随账号删除。

## 集成位置（由主任务统一处理）

1. `backend/db/base.py` 在既有注册 import 后加 `from backend.db import business_config_models as _business_config_models`。这使 Alembic metadata、create_all、reset 的 Base 完整表库存都含两张新表；不能仅靠 API 被导入时注册。新服务也导入模型，单独业务测试可直接使用。
2. 私有 `backend/private_main.py` 挂载新 router，保持现有 `/api` 前缀；公开 main/allowlist/bundle 不增加入口。本任务测试用独立 FastAPI app 挂载 router，未修改共享入口。
3. 私有 admin-main 路由指向新页面，AdminShell 加“业务配置管理”；用户详情可链接 `/admin/business-config?userId=<encoded-id>`。
4. 迁移单 head `0023_business_configuration`，down_revision=`0022_account_closures`。主任务将两张新表纳入全清空固定库存/测试，用户销户可显式删除 `user_storage_configuration` 或依赖 FK CASCADE（当前仓库通常使用显式库存，需加入）。全清空删除 `business_configuration` 与 `user_storage_configuration` 后恢复环境规则；不删除部署参数/密钥。
5. 主任务统一覆盖私有入口隔离、reset/closure 完整库存、PostgreSQL 迁移/并发与浏览器联调，并统一提交。此文档与本任务检查点需由主任务纳入 canonical 导航；本任务不改共享索引或 PROJECT_MEMORY。

## 验证与边界

新增后端测试使用隔离 SQLite、合成账号、模拟 sender，覆盖未配置兼容、严格校验、域名边界与验证时规则、旧账号找回、当前窗口邮件限制、管理员协助限流、权限/审计/幂等/版本冲突、双线程写入防覆盖、单用户继承/降额/零值/释放、跨进程立即读取，以及 SQLite migration upgrade→downgrade→upgrade。前端测试覆盖数值/空值语义、继承、保存/冲突/安全重试、用户搜索/直达和未保存离开提示。

实际结果见本任务最终报告/导航检查点。未执行真实邮件、模型、生产数据、AWS/Cloudflare、push/PR/merge/deploy；原有生产安全门禁仍独立适用。

## 本任务交付与验证记录

2026-10-02：指定既有 clone，分支 `codex/admin-completion-20261002`，基线 HEAD `164e614ecddb4e1a735dbcb5fe53212126972351`。本任务没有 stage/commit/push，共享入口的并行修改属于主任务。

- 最终定向后端组合：142 passed（67 warnings），包括本文件新增的 38 项配置用例及既有邮件/存储回归；仅隔离 SQLite 和模拟 sender。
- 新增前端：2 files / 9 passed；TypeScript typecheck、git diff --check 通过。
- 新增测试已包含独立长驻 Python 进程对 DB 变更的即时读取、双写竞争、管理员协助邮件限流和 SQLite 迁移往返。
- PostgreSQL、最终 reset/closure 联合库存及浏览器验证由主任务统一执行，本任务不使用其服务或进程，也不声称这些验证已完成。

本任务独占文件（共 14 个，以下均相对指定 clone 根）：

- `backend/db/business_config_models.py`
- `backend/db/migrations/versions/0023_business_configuration.py`
- `backend/services/business_config.py`
- `backend/api/admin_business_config.py`
- `backend/tests/test_admin_business_config.py`
- `backend/services/email_registration.py`
- `backend/services/password_reset.py`
- `backend/db/source_storage_repository.py`
- `backend/db/knowledge_storage_repository.py`
- `frontend/app/src/api/adminBusinessConfig.ts`
- `frontend/app/src/api/adminBusinessConfig.test.ts`
- `frontend/app/src/routes/admin/AdminBusinessConfigPage.tsx`
- `frontend/app/src/routes/admin/AdminBusinessConfigPage.test.tsx`
- `docs/ADMIN_BUSINESS_CONFIG.md`


## 2026-10-03 模型日额度补齐

复用原 business_configuration / user_storage_configuration 的 JSON 覆盖和版本。
后者保留历史表名，现允许三项模型键；不是第二套配置来源。优先级仍是用户覆盖 →
全局覆盖 → 部署 settings → 代码默认；每次准入读取提交的 DB 快照，多进程无缓存。

| 项目 | 默认/兼容值 | DB 覆盖范围 |
| --- | --- | --- |
| shared_pool_daily_request_limit | 原部署值，代码 100 | -1 到 1000000 次 |
| shared_pool_daily_estimated_token_limit | 原部署值，代码 100000 | -1 到 1000000000 估算输入 token |
| history_query_llm_daily_limit | 原部署值，代码 20 | -1 到 1000000 次 |

DB 的 -1 明确无限制；0 禁止新调用，null 恢复继承。历史部署负数继续解释为 0，
不把未设置 DB 配置的部署意外放宽。共享池开关、历史 Ask 开关及冷却秒数仍是部署
参数；配额后台不能打开共享模型池。普通 BYOK 批改不计共享池额度，任务 Ask（无论
BYOK 或共享）沿用原来的独立调用上限；共享 Ask 同时经过两项准入检查。

0024_model_daily_usage 在原单一 head 0023 后新增 owner/scope/UTC day 的持久化账本，
保留已发布迁移。SQLite BEGIN IMMEDIATE、PostgreSQL owner row FOR UPDATE 在同一
事务内查有效上限并扣数，提交后才调用供应商。日边界统一 UTC 00:00；重启不清零，
新 UTC 日使用新行，历史 Ask 冷却也跨进程持久化。管理员调整额度不改写已有计数。

计量是“已准入逻辑调用”，失败/取消/进程中断不退款；供应商 SDK 在同一次 ainvoke
内的内部重试只计一次，用户重新发起的实际模型调用计新一次。现有任务幂等策略继续
阻止相同业务操作重复启动模型。没有需要事后退款的预占/结算状态，也不会因崩溃释放
已耗额度。失败前扣除是保守的原有语义，不能将其宣传为实际成功调用统计。

估算输入 token 保留 content 字符长度合计 /4（最低 1）的历史启发式，不是供应商
真实输入/输出 token，也不估算费用。存量进程内历史计数不能补采，迁移日起开始可靠
持久化；旧运营事件不会被改成完整模型历史。

教师账户设置和“模型与 BYOK”可刷新实际用量与上限；超额提示 UTC 重置、联系管理员
或使用 BYOK。历史 Ask 额度不足时保留确定性关键词查询。降低额度不删除已有数据或
停止历史批改任务，只拒绝后续不满足额度的模型准入。账号销户级联删除自己的账本；
全站清空连同全部用户覆盖、全局覆盖和计数删除，回到部署初始规则。
