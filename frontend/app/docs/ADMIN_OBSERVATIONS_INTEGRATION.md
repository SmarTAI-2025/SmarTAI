# 管理端运行监控与运营统计：集成交接

日期：2026-10-02。来源：[开发 SmarTAI 管理端运行监控与运营统计](codex://threads/01a0fcbf-420e-7911-b75e-0d1766f6042c)。主任务：[完善管理员功能并审查 PR #117](codex://threads/01a0fc96-75da-7f73-8f70-3e2c780d8596)。

## 当前交付边界

- 独立 clone：`/Users/annie/code/SmarTAI/tmp/admin-monitoring-20261002`。
- 分支：`codex/admin-monitoring-analytics-20261002`，基于 PR117 `8d6277002b28a6948ba8f083185ac77df8e7b884`。
- 只新增专用后端模块、页面、API 客户端、测试和本文；没有修改共享认证、`backend/api/admin.py`、用户模型、既有迁移、`AdminShell` 或 `admin-main.tsx`。
- 本地实现和独立页面验证完成；尚未接入实际管理端入口/导航。PR117 与 main 的整合由主任务执行。本任务未推送、开 PR、合并、部署或操作外部服务/真实数据。
- 没有新增后台监控进程。运营页面不读取作答、资料正文、姓名、邮箱、密钥或用户身份列表，不调用模型。

## 主任务需要的准确接线

1. 将本分支的新增文件提交 cherry-pick 到主任务的 PR117 + main 集成分支。新模块依赖 PR117 的 `AdminUsageEventRecord` 和现有文件元数据模型；先保留该事件表及其既有迁移。本任务无需新迁移。
2. **仅**在 `backend/private_main.py` 导入并注册：

   ```python
   from backend.api.admin_monitoring import router as admin_monitoring_router
   # create_private_app() 内，沿用既有 private-enabled 开关和认证配置
   app.include_router(admin_monitoring_router)
   ```

   不在 `backend/main.py`、公开代理 allowlist 或公开 OpenAPI 中注册。现有私有 CORS 的 GET/POST 已覆盖两条接口。
3. 在 `frontend/app/src/admin-main.tsx` 导入：

   ```tsx
   import { AdminMonitoringPage } from "@/routes/admin/AdminMonitoringPage";
   import { AdminAnalyticsPage } from "@/routes/admin/AdminAnalyticsPage";
   ```

   在已有 `RequireAdminSession` 包裹的 `/admin` 子路由加入：

   ```tsx
   { path: "monitoring", element: <AdminMonitoringPage /> },
   { path: "analytics", element: <AdminAnalyticsPage /> },
   ```

4. 在 `AdminShell` 的桌面和移动导航共同使用的 `links` 中加入明显入口：`/admin/monitoring`「运行监控」和 `/admin/analytics`「运营统计」，图标可用 `Activity` / `LineChart`。主任务统一导航空间和账号设置安排；不要向普通教师导航增加这两个入口。
5. 管理端构建的 `VITE_SMARTAI_BACKEND_URL` 指向私有管理员 API。不能因为页面有权限保护，就将私有 API 暴露到公开站点。沿用当前 QueryClient、Providers、认证失效处理和退出时私有缓存清理。

### 接口

| 方法与路径 | 输入 | 结果 / 边界 |
|---|---|---|
| `GET /admin/monitoring` | 无路径、SQL 或配置输入 | 当前检查、账面字节、分区容量、近 7 天样本、阈值建议 |
| `POST /admin/analytics/query` | `start` Unix 秒；可选 `end`（默认服务端当前时间）；`timezone`（默认 Asia/Singapore） | 增长、业务/登录活跃、频率、成熟 W1/W4 群组 |

两条接口均复用 `require_admin`，拒绝匿名、非管理员和现有认证规则认定无效的账号；成功响应 `Cache-Control: no-store`。统计时间区间为 `[start,end)`，区间长度最多 93 天，结束不得超过当前时间；时区经 ZoneInfo 校验。最多载入 100,000 条区间事件和 100,000 个首次动作用户，超过时明确 422，不能把截断数据当完整统计。数据库异常返回通用 503，不返回连接串。

## 监控口径和运维配置

- 私有应用：只证明本次请求正常响应。公开应用、队列/worker 与模型服务没有探测，不推断整体服务健康。
- 数据库：现有引擎 `SELECT 1`，不能证明所有业务表或写入正常；连接/查询超时继承现有数据库引擎配置。本功能没有新增数据库连接超时保证。
- 本地存储：检查目录及读/写/执行访问权限，不写探针文件。对象存储：使用现有显式 S3 凭据执行 HEAD bucket，连接/读取各 2 秒、一次尝试；不列对象、不读写正文。没有显式凭据时显示不可用，不退回实例元数据查询。真实 S3、IAM/网关和部署挂载仍须运维验收。
- 应用账面字节：`StoredFileRecord` 的非 unavailable 对象，加 `KnowledgeStorageRecord` 的 available / cleanup_pending 对象，按 storage backend + key 去重取最大字节。预留知识容量单列；孤儿对象、上传中数据、旧版本、数据库、日志和备份不包含。此值不是 bucket 实测或账单，也不一定属于采样磁盘。
- 分区容量：`shutil.disk_usage` 实测当前进程可见分区。默认 local 使用存储目录，object 使用进程当前目录；不声称它是宿主整盘。
- 可选环境项 `SMARTAI_MONITOR_HOST_DISK_PATH`：仅运维配置，指定已验证的宿主分区挂载路径。客户端不可传路径。配置后标记 operator_configured，仍不声称系统自动证明了挂载正确。响应不会暴露绝对路径/hostname。
- 打开/刷新监控才采样，同一 scope 每 300 秒最多保存一条。复用事件表的 `admin_capacity_sample_v1`，不包含 user_id；scope 的 hostname/路径/device 信息只以哈希存储。同一稳定 scope 可跨服务重启保留；容器 hostname/路径变化会开始新 scope，不拼接不同机器曲线。
- 技术样本保留 90 天，只删除该事件名的过期样本；不删除产品事件/审计。图显示近 7 天，超过 15 分钟的断档不连线。历史 unavailable 与首次采样区分。
- 预测只用最近同容量段：跨度至少 24 小时、至少 3 点、相邻间隔不超 6 小时、最新样本距当前不超 10 分钟；按可用空间净减少作线性估计。无增长没有耗尽日期；扩容后重新积累，不保证未来容量。
- critical：已用 ≥90% 或可用 <1 GiB；warning：已用 ≥80%、可用 <2 GiB 或估计 <14 天。仅建议核对清理和扩容，不执行操作。

## 运营指标与历史边界

| 指标 | 精确定义 |
|---|---|
| 新增教师 | 成功、事件角色 teacher 的 `account_created` 去重用户数；当前教师账号存量另查 UserRecord，含停用账号，不回填历史 |
| 登录活跃 | 区间内 `login_success` 去重教师 |
| 业务活跃 | 区间内成功的 `task_created`、`course_created`、`assignment_created`、`grading_run_started` 至少一次，去重教师；批改启动不等于完成或教师确认 |
| 使用频率 | 每位业务活跃教师在选定日历时区有业务动作的天数，1 / 2–3 / 4–7 / 8+ 天；另列总动作数 / 区间活跃人数 |
| W1 / W4 | 首次保留业务动作作为代理起点，分别观察相对时间 `[7,14 天)`、`[28,35 天)` 中有业务动作的用户；分母仅完整经历窗口者，待观察人数另列，零分母显示尚未成熟 |

最早保留事件不是采集部署起点。事件尽力写入可能缺失，所以始终标记 partial；起点之前每日值为 null，完全没有事件显示不可用，不生成历史。之后的 0 只代表没有已记录事件。时区用于日历桶/群组日期，留存窗口使用实际经过秒数；查询结束是观察截止。

未分类内部测试与真实试用账号，页面明确包含全部未分类教师。历史事件保留已删除/停用账号，避免从留存分母抹去流失用户。不将此代理留存称为已确认教学价值留存，不推导注册转化、完整漏斗或付费增长。

## 验证证据与剩余工作

- 2026-10-02：4 个管理员后端测试文件共 **46 passed**；随后增加对象 HEAD / 凭据保护 / 安全 503 测试，最终新文件 **33 passed**（与前组重叠 30 项；另 16 项既有回归）。全部使用一次性 SQLite、临时上传目录和假凭据。
- 两个新增前端测试文件 **8 passed**；验证筛选/时区/刷新/重试、未知与零、未成熟分母、失败不展示旧成功，以及 API 方法与取消信号。
- `npm run build`、`npm run build:admin` 均通过含 TypeScript 检查。标准管理入口尚未导入新页，所以另用临时 QA 入口对两张真实页面执行 production build 和 Chromium 验证。
- 隔离 HTTP API 使用真实 JWT 管理员鉴权，合成教师/事件；容量读取本机 QA 目录所在分区。浏览器验证 1440×1080 和 390×844，图表、区间与时区切换、刷新、错误隐藏旧成功和重试恢复均通过；无页面运行错误、无 Vite overlay、页面无横向溢出。表格在卡片内横向滚动。QA 外壳不代替主任务的最终导航/认证集成验证。
- 临时证据目录：`/private/tmp/admin-monitoring-qa/`，包括 `browser-result.json`、五张截图与后端日志；导航交付记录保留其副本。QA 入口与账号数据不提交。
- 已有构建仍有 >500kB chunk 提示，安装 PR117 锁文件时 npm 报 1 项 high 依赖提示；本任务未升级依赖、未把锁文件视为安全审计结论。
- 待主任务：接线并对 PR117 + 最新 main 候选重跑相关验证；检查公开路由/OpenAPI/教师 bundle 不泄漏私有功能；验证私有登录、退出缓存、导航和多角色拒绝。PostgreSQL 实测、真实 S3/宿主挂载、连续容量样本、真实运营数据和用户验收均 unverified。生产前仍遵守 `docs/active_beta_launch/20260803_leader_replan/PRE_PRODUCTION_SECURITY_RELEASE_GATE_CN.md`。
