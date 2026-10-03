# 完整业务清空：离线维护工具

2026-10-02；仅实现与隔离数据验证，不代表生产、真实对象存储或部署验收。

2026-10-03 更新：不再用 development/test/production 的名称决定是否允许清空。
统一使用管理员身份、独立维护密码、精确目标确认、项目专用资源标记和离线锁。
**实现和测试获授权；真实 AWS、SMTP、S3 操作与线上配置变更未获授权，均未执行。**

普通管理页面的“系统维护”仅预览。先停止全部写入服务，然后在同一主机、相同数据库/
存储/密钥配置下启动专用维护服务，访问 `/maintenance` 执行按钮。维护服务不挂教学、
账号管理、配置修改、密码重置、邮件发送等业务接口，不启动后台 worker。仅登录授权
会写入独立管理员会话；清理中连登录也暂停。服务不是网络隔离措施，必须绑定回环地址，
通过 SSH 隧道或 VPN 访问；禁止用公网网关直接发布。

```bash
python scripts/hash_maintenance_password.py
# 将新哈希以部署秘密配置为 SMARTAI_ADMIN_RESET_PASSWORD_HASH；不要写入业务库。
# 不改变已有 JWT、BYOK 主密钥、SMTP 密码或 Parameter Store 配置。
python -m uvicorn backend.maintenance_main:app --host 127.0.0.1 --port 8001 --workers 1
```

维护凭据是额外的一项密码，不授予普通用户管理员权限。初次执行仍验证目标数据库中的
真实管理员身份、有效会话、auth_version、活动状态，并在管理事务中重新验证。独立
密码以 bcrypt-SHA256 哈希保存；API 使用 SecretStr，校验错误不回显原始输入。密码
失败 5 次后暂停 15 分钟，不写入前端草稿、URL、业务审计或维护回执。

页面预览显示环境、数据库脱敏标识、本地目录、对象范围摘要、表/文件/版本/分段上传
数量及不可逆影响。管理员输入独立维护密码和本次精确短语，勾选“全部普通服务已停止”，
然后确认。后台重新核对目标、清单和离线锁；预览失效、密码错误、权限不足、其他进程
仍运行均拒绝。没有单独的“生产绕过开关”。运行开关仍默认关闭，专用资源未登记时
失败关闭；这属于项目范围保护，不是环境名限制。

维护服务保留文件阶段/数据库阶段/失败代码及残留数量。操作凭据是独立 HttpOnly、
SameSite=Strict Cookie，作用域限定 `/api/admin/maintenance`，24 小时有效，只能查看
和恢复其已授权的精确计划；恢复仍需独立维护密码。同源校验防止跨站触发。即使所有
用户已删除，也能展示完成状态，刷新不丢失维护记录。进程重启后用相同记录恢复，
不会因新预览或重复点击删除重新注册的数据。多 worker/另一个维护进程会被 POSIX
及 PostgreSQL 控制锁拒绝。SQLite 只支持同一维护主机；所有实际写入者必须遵守
生命周期锁，部署前要核实外部 worker/调度器也已停止。

CLI `scripts/reset_business_data.py` 保留预览和恢复入口。执行时隐藏提示输入管理员
自身密码与独立维护密码；已持久化授权的精确恢复计划可以在用户已清空时恢复回执。
未知计划不能靠维护密码跳过首轮管理员身份检查。授权文件与回执位于业务库外的专用
维护目录，必须当前运维用户所有且不可被组/其他用户写入。

## 清空范围与保留项

| 存储位置 | 处理 |
| --- | --- |
| 当前数据库的全部 `Base.metadata` 业务表 | 全部清空，包括没有外键的表；动态纳入新增表 |
| 身份、安全、管理表 | 用户（包括全部管理员）、刷新会话、验证/密码重置记录与限流、邀请、账户关闭、封禁邮箱、业务审计与使用事件全部清空 |
| 教学、工作流、知识库、提供商配置 | 课程/成员、作业/结果、工作流/来源/结果、教材/知识库/chunks、上传记录/存储预留/孤立追踪、用户 BYOK 加密记录全部清空 |
| 本地存储 | 所有登记的专用目录内文件和孤立文件全部删除；目录本身及安全标记保留 |
| S3 专用 bucket | 逐版本物理删除，包括历史版本、`null` 版本、删除标记；中止全部未完成 multipart uploads；重新完整列举验证为空 |
| 内存缓存、任务、连接与子进程 | 所有服务/worker/调度器先停止，确认子进程结束；完成后启动全新进程 |
| schema、约束、索引、`alembic_version` | 保留；不删除/重建数据库，不创建或改迁移 |
| 基础设施、环境配置、部署密钥、bucket 配置 | 保留；用户数据库中的 BYOK 属于业务数据，不在保留项内 |
| 外部维护回执 | 保留操作 ID、时间、环境/范围哈希、计数、结果；不含用户名、邮箱、内容、对象路径或凭据 |

`backend.db.base` 已导入 `models`、`workflow_repository` 和
`source_outcome_repository`，因此这些仓库单独声明的表也被纳入。
实际数据库表集合必须与代码 metadata 完全一致（仅排除 `alembic_version`）；
未知表、缺失迁移、无法识别的历史 storage backend 会拒绝执行，避免误删配置表或
遗留未经盘点的对象。SQLite 删除后验证外键，并执行 secure-delete/VACUUM/WAL
checkpoint；PostgreSQL 在同一事务内对完整表集合执行 TRUNCATE RESTART IDENTITY，
不使用扩大到外部表的 CASCADE。

全部身份及注册占用记录清空后，同名用户名/邮箱可重新注册。
清空不会保留管理员例外，也不会开放管理员自行注册。

## 必须先完成的维护接线

所有 public/private 服务、worker、定时器必须在**整个进程生命周期**持有：

```python
from backend.services.admin_reset import maintenance_service_guard, scope_from_settings

with maintenance_service_guard(scope_from_settings()):
    # 整个服务生命周期；先关 worker/任务/子进程，再退出此上下文
    run_service()
```

ASGI 应在 lifespan/startup 获取上下文，在完整 shutdown 后释放。仅调用一次
`assert_reset_not_in_progress(path)` 不足以关闭启动与执行之间的竞态。
`scope_from_settings()` 在 object 模式没有客户端时仍可供 guard 使用；guard 不枚举
对象。真正 preview/execute 没有显式客户端会返回 `reset_object_client_required`。

所有进程与 CLI 必须使用同一绝对路径 `SMARTAI_ADMIN_MAINTENANCE_DIR`；目录不可位于
上传目录、数据库内部或可被清空的根中。POSIX flock 提供共享服务锁/独占执行锁；
PostgreSQL 还使用数据库级共享/独占 advisory lock。`active-reset.json` 是跨崩溃
维护标记，存在时即使进程锁释放也不允许服务启动。跨主机运维还必须保证每台主机
可见同一维护状态目录；不支持用彼此不可见的本地 marker 假装完成集群维护。

工具不会自动停止服务，也不能检测所有第三方直接数据库/对象存储写入者。
`--services-stopped` 是运维人员的明确确认，不能代替停掉所有部署副本、旧容器、
进程管理器自动重启、定时器、邮件发送任务、解析/OCR/批改 worker 及其子进程。
执行期间禁止任何外部上传或旧服务重连。

## 专用目录和临时文件盘点

本地存储根必须是只放该环境业务字节的目录，内含安全标记：

- 文件名：`.smartai-disposable-storage`
- 精确 JSON：`{"schema_version":1,"purpose":"smartai-disposable-business-storage"}`

操作者只能在确认目录归属后创建标记；预览不会自动创建标记或维护目录。
工具拒绝根目录、home、仓库根、共享 tmp、配置/密钥/数据库混入、符号链接、硬链接、
特殊文件、挂载跨界、上传目录包含数据库、维护目录与上传目录互相包含等情况。
若某个用户上传文件恰好叫 `.env` 或 `.db`，也会保守拒绝；应先人工核对，不能去掉
检查来迁就未盘点的目录。macOS `/tmp` 是符号链接，隔离测试使用 `/private/tmp` 的
专用子目录，绝不能登记 `/private/tmp` 本身。

除 `SMARTAI_STORAGE_ROOT` 外，使用 JSON 数组环境变量
`SMARTAI_ADMIN_RESET_EXTRA_STORAGE_ROOTS` 明确登记专用临时目录、过去使用过的本地
存储根，每个目录都要安全标记；不允许重复或嵌套根。

`code_interpreter.py` 会写临时代码，`file_processing.py` 的 7z 解压会写临时文件；
正常返回通常清理，但崩溃可能留下字节。因此 reset 启用前，所有应用进程应统一使用
**独立且持久存在的 `TMPDIR`**，该路径必须本身是清空范围中的一个根。CLI/settings
适配器缺少这一配置会拒绝预览和执行。建议 TMPDIR 与 uploads 并列，避免清空嵌套
目录后 Python 静默回退到系统 tmp。object 模式也需要这个本地临时根。
历史上若使用过共享系统 tmp，无法安全识别归属的文件不可自动删除；先由运维盘点，
不能把该状态宣称为所有历史业务副本已清空。工具不扫描共享系统目录。

示例配置**仅用于专门准备的可丢弃测试环境**，占位符必须替换并人工核对；本说明不
创建目录、不读取真实配置、不执行清空：

```text
SMARTAI_RUNTIME_ENVIRONMENT=test
SMARTAI_ADMIN_RESET_ENABLED=true
SMARTAI_ADMIN_MAINTENANCE_DIR=/absolute/disposable-env/maintenance
SMARTAI_STORAGE_ROOT=/absolute/disposable-env/uploads
TMPDIR=/absolute/disposable-env/runtime-temp
SMARTAI_ADMIN_RESET_EXTRA_STORAGE_ROOTS=["/absolute/disposable-env/runtime-temp"]
```

数据库仍使用应用原有 `SMARTAI_DATABASE_URL` / LIGHT / HEAVY 及数据库模式设置；
CLI 不另造一个可与服务锁指向不同实例的数据库参数。确保当前工作目录和应用配置
相同，不要把数据库 URL、密码或密钥复制到工单、日志或命令输出。

## 只读预览、确认和执行

1. 先盘点所有数据库、当前/历史本地根、对象 bucket、专用 TMPDIR 及部署副本。停止
   自动重启和所有服务/worker/子进程，并保持离线。检查预览与当前环境是否一致。
2. 使用项目 Python 3.12 及已安装的共享 hash lock 依赖运行：

   ```bash
   python scripts/reset_business_data.py
   ```

   返回逐表计数、文件/版本/分片计数与字节数、安全范围说明和限制。字节总数不包括
   未完成 multipart 中的片段。文件内容、数据库行内容、对象 Key、URL、密钥不返回。
   预览不写 marker/receipt、不删任何数据；管理员 UI 可调用同一函数。
3. 审查并保留返回的完整 `fingerprint` 和 `confirmation`。fingerprint 含环境/数据库/
   存储位置哈希、schema/计数/文件清单摘要和本次预览随机 ID；不只是勾选框。
   环境或清单改变会要求重新预览。行计数摘要不是所有行内容的快照，因此必须先停止
   全部写入者。每次新预览具有不同 ID；执行成功的旧命令不会再清空后来注册的数据。
4. 只有获得目标环境实际清空的单独授权后才执行精确预览：

   ```bash
   python scripts/reset_business_data.py --execute --services-stopped \
     --fingerprint '<完整fingerprint>' \
     --confirm 'ERASE ALL DISPOSABLE BUSINESS DATA <fingerprint前12位>'
   ```

   不加 `--execute` 永远只是预览；取消、短语不符、漏掉离线确认、过期范围，均不删
   数据。可通过上述离线维护页面执行，或使用 CLI；维护进程必须拥有目标 DB/文件系统权限。
5. 检查 `status=completed`、`bootstrap_required=true` 和外部维护回执，复核新预览的
   全部计数为零。此时 schema/配置仍在，全部历史管理员已清空。
6. 保持公众入口关闭，使用本机受控首管理员 CLI：

   ```bash
   python scripts/create_admin.py '<新管理员用户名>' --email '<管理员邮箱>'
   ```

   密码从隐藏终端提示输入，不放命令参数。此 CLI 不发送邮件、不会提升既有用户；
   已存在管理员时拒绝再次 bootstrap。创建成功后再启动全新服务进程，确认维护 guard
   工作，随后恢复测试访问。数据库身份检查和被清空的刷新会话使旧登录失效，不能保留
   旧进程内的 provider registry、识别字节缓存、RAG 检索器或任务引用继续服务。

## 对象存储边界

CLI 必须显式加 `--allow-object-storage`；维护服务必须配置
`SMARTAI_ADMIN_RESET_ALLOW_OBJECT_STORAGE=true` 才构造 S3 客户端，预览也会读取对象安全配置。当前测试全部使用 fake client，未对 AWS、Cloudflare/R2 或其他真实
服务发出对象请求，真实 IAM、endpoint、API 兼容性和物理删除语义未验证。

必须是该环境独占 bucket，不支持共享 bucket 的 prefix 清空。bucket 必须已有标签
`smartai-reset-scope=smartai-exclusive-business`（兼容既有 `disposable` 标记）。权限须能完整读取 tag/versioning/replication/object-lock
配置、列举所有版本与对象、列举/中止 multipart、删除指定版本。存在 replication、
Object Lock、MFA Delete，或兼容接口不能证明这些配置不存在时一律拒绝。不能把普通
DELETE 返回成功当作历史版本已删除；删除完成后重新枚举，发现残留就保留维护状态。
权限配置和安全标记必须由另行授权的运维操作完成，本工具不修改 bucket 或 IAM。

数据库若仍追踪另一种 storage backend 而不在当前范围内会失败。历史 bucket 切换无法
仅从 backend 名称推断，必须先盘点迁移历史；当前 CLI 只接受一个已配置的完整 bucket，
多个历史 bucket 需要先制定并验证明确范围，不可宣称单次调用已处理未知旧 bucket。

## 失败、重试与审计

先取得维护独占锁和数据库写锁，再把不含真实身份的 plan 写入
`active-reset.json`，然后删除文件/对象，最后清空业务数据库。存储删除失败时数据库
事务回滚，业务行保留；已删除文件不可回滚，服务必须继续离线。修复本地权限/空间或
经授权修复对象访问后，使用**相同 fingerprint 和 confirmation** 重跑。原 plan 只
允许清单减少；新增文件、对象、行数/schema/环境变化会中止恢复。

DB 失败会回滚业务行；文件仍可能已经删去。提交后、receipt 写入前崩溃可以在验证
数据库及存储全部为空后补回执。成功回执持久化后，重复命令只返回 `replayed=true`，
不会删除新数据。不要手工删除 active marker、修改 plan、拿新预览覆盖未完成计划或
提前启动服务；对不一致状态应保留维护证据并人工排查。

receipt 位于维护目录 `receipt-<fingerprint>.json`，包含操作 UUID、起止时间、范围
哈希、删除总行数与存储计数、结果；业务审计表同样被清空。维护目录权限收紧，文件
原子写入并 fsync。错误只记录稳定错误码，不保存异常原文、对象路径、身份或凭据。
维护回执的保存周期由运维政策确定；本工具不自动删除它们。

此功能完成的是应用业务状态重置，不是磁盘取证级擦除。外部备份、快照、WAL 归档、
云厂商保留副本、第三方模型副本、已经发送的邮件/日志、浏览器下载和人的副本不在
自动清空范围内；不能把应用计数为零描述为这些副本也已销毁。

## 验证记录

本次最终实测：SQLite + 专属 UTF8 PostgreSQL 合计 **68 passed, 2 skipped（20.99s）**。
两项 skip 分别是 PostgreSQL 不适用的 SQLite 历史坏外键 fixture，以及 SQLite 不适用的
PostgreSQL advisory 锁测试。三份 Python 文件语法编译通过。

独立测试命令：

```bash
python -m pytest backend/tests/test_admin_reset.py -q
# 可选：仅允许名称 smartai_reset_disposable、host 127.0.0.1 的临时 PostgreSQL
SMARTAI_RESET_TEST_POSTGRES_URL=postgresql+psycopg://smartai_admin_test@127.0.0.1:55437/smartai_reset_disposable \
  python -m pytest backend/tests/test_admin_reset.py -q
```

PostgreSQL fixture 会重建**该专属测试库**的 public schema；禁止指向应用实例。
临时 PostgreSQL 使用 UTF8（`template0` 初始化），避免 SQL_ASCII 的 psycopg bytes 行为。
测试覆盖身份与无 FK 记录释放、循环知识库/工作流图、孤立文件、专用临时根、存储/
数据库失败续跑、提交后恢复、取消、不同进程与 PG advisory 锁、重复成功命令、新文件
拒绝、危险路径、默认关闭、CLI 只读预览、S3 版本/删除标记/null 版本/分页/multipart。
完整项目 API/UI 接线、浏览器行为、部署和真实服务验收由主线程单独记录，不能用本
模块测试代替。


## 部署资源登记与完成后的重新初始化

- 明确启用 `SMARTAI_ADMIN_RESET_ENABLED=true`，维护目录必须在业务根之外。
- `SMARTAI_STORAGE_ROOT`、`TMPDIR`（独立临时目录）、额外历史本地业务根
  `SMARTAI_ADMIN_RESET_EXTRA_STORAGE_ROOTS`（JSON 数组）均须登记；禁止共享 `/tmp`、
  symlink、hardlink、跨挂载边界和含部署配置/数据库的目录。
- 专用本地根保留 `.smartai-disposable-storage` 兼容标记；新登记的内容为
  `{"schema_version":1,"purpose":"smartai-exclusive-business-storage"}`。
  名称只是历史兼容，环境名称不影响授权。挂载盘必须使用其内部的项目专用子目录，
  不能把整块共享挂载根当作项目范围。
- 对象存储仅支持当前配置的完整独占桶。共享桶、多历史桶、外部备份、replication、
  Object Lock、MFA Delete 不支持自动清理；检测到不确定范围时拒绝，不能宣称清空完成。
  工具不创建标记、改 IAM、bucket 配置、数据库实例或其他基础设施；资源登记需独立运维授权。
- 清理动态纳入 `model_daily_usage`、业务配置、用户覆盖与黑名单。完成后全部业务表零行、
  本地根只留登记标记、对象版本/删除标记/multipart 都为空，才生成成功回执。
- **停止维护服务**，保持公众入口关闭；运行 `python scripts/create_admin.py USER --email EMAIL`，
  隐藏输入新密码。只允许无管理员时初始化一个新账号，不提升既有普通账号。
- 核对部署继承的注册域名、邮件限制和模型/存储初始值，再启动全新 public/private/worker
  进程，测试登录与业务 API、私有站点和公共入口隔离，最后开放注册。同名/同邮箱及旧
  黑名单邮箱不再有身份占用；注册仍遵守部署初始域名规则。

回执只含 ID、时间、范围摘要、计数和结果。短期授权文件另含令牌哈希、授权时间和
不可逆管理员 ID 摘要，用于恢复精确计划，不含原始令牌、密码、用户名或邮箱。普通
应用审计随业务清空删除；维护文件保留在独立控制目录，不参与新的运营统计。
