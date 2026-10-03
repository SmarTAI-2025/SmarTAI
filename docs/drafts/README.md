# 显式本地暂存与离开保护

[逐页覆盖清单](PAGE_INVENTORY.md)逐项区分已启用业务页面、即时正式保存、展示、敏感信息及未启用代码。PR #132 接续原有 owner/task/reference/late-response 保护，将自动 sessionStorage 保存替换为单一的显式 IndexedDB 快照；旧自动快照清理，不混用两套机制。

## 实现与生命周期

- `useDraftProtection` 适配既有状态，`usePageDraft` 管理简单表单，`DraftField` 隔离每个独立正式保存字段；`useDraftLeave` 统一站内路由、弹窗/浮层、主动刷新/退出意图和原生 beforeunload。恢复的是最后一次明确暂存；后来输入不自动覆盖它。
- 字段与 File/Blob 的实际 ArrayBuffer 在一个 IndexedDB 读写事务提交。仅事务完成后显示“已暂存”和时间；容量、文件读取、存储或并发失败均保留工作区与旧版本。浏览器持久化失败不阻止编辑和原有正式保存。
- 键隔离现有登录用户、页面、任务、学生/题目、资料对象；课程/工作流或正式字段版本作为业务冲突依据。恢复前版本不一致时保留正式值与草稿，用户核对后选择恢复或删除。
- 已上传题目/资料库引用及教材预检引用恢复只发 GET 校验 owner/task/实际对象、状态、TTL/版本；不上传、不预检、不识别、不批改、不延长服务器引用寿命。教材与题目在正式启动失败后手动重试复用有效预检；无效引用说明原因并允许重选/重新开始。
- 站内未暂存离开三选项，保留原目标且只继续一次。暂存失败留在原页面；期间又修改不继续离开。业务保存期间保持既有并发锁与保护。复核“正式保存/确认”仍是原业务操作；“暂存并离开”没有确认效果。
- 正式成功清除对应快照，失败不清；保存一个题目字段不清除兄弟字段。墓碑版本、事务 CAS 与登录清理 epoch 防止迟到写入复活或多标签页覆盖。多标签页变化提示核对，冲突写入被拒绝。
- 明示保留 7 天，单页事务最多 64 MiB，总计 128 MiB / 30 个业务快照；设置及资料库提供发现/打开/删除入口。过期/旧格式/损坏数据不伪装可恢复。登出、账号切换、会话失效清理本机快照；共享设备不应依赖浏览器暂存长期保留个人信息。
- 认证密码、验证码、重置 token、BYOK API key/secret 不进入草稿库；这些编辑仅内存保留、原生提醒和“继续/放弃”站内保护，继续使用原认证和密钥保存协议。原已有 auth token 存储不由本功能改造。

## 浏览器边界

明确暂存后的字段和真实文件可在同一浏览器正常刷新、关闭后重开时恢复，受 7 天期限与浏览器实际存储保留约束；不承诺跨设备、隐私模式结束、浏览器清理/驱逐数据或永久保存。未点击暂存的新修改仅在当前工作区；原生确认选择留下时输入仍在，可再显式暂存。

有未暂存修改时注册 beforeunload；成功暂存/无改动即移除，业务保存中的既有保护保留。原生按钮/文字由浏览器控制，没有自定义“暂存”按钮，不在关页时异步保存。桌面 Chromium、Firefox、WebKit 引擎实测刷新、标签关闭、跨文档跳转的确认与取消，站内后退/前进使用应用三选项。Playwright `runBeforeUnload` 与 `location.reload/assign` 是本次原生验收触发方式；没有据此声称所有工具栏/系统关闭方式、所有浏览器百分之百有效。手机强杀、崩溃及缺少用户激活可能不提示；手机宽度验收不是手机系统强杀拦截证明。WebKit 验收不等同真实 Safari。

## 可复现验证

使用没有生产 `.env` 的独立 checkout、临时 SQLite/上传目录、隔离 demo 账号。`scripts/drafts/qa_backend.py` 拒绝非 test/非临时 SQLite，所有 provider 构造替换为禁止外部调用的测试实现。下面从仓库根启动后端；显式环境覆盖实际秘密，仅测试占位值：

```sh
QA_DIR=$(mktemp -d /tmp/smartai-draft-qa.XXXXXX)
env -i PATH="$PATH" PYTHONPATH="$PWD/frontend/app/scripts/drafts:$PWD" \
 SMARTAI_RUNTIME_ENVIRONMENT=test SMARTAI_GRADING_ENGINE=v2 \
 SMARTAI_DATABASE_HEAVY=OFF SMARTAI_DATABASE_AUTO_CREATE=true \
 SMARTAI_DATABASE_URL="sqlite:///$QA_DIR/test.db" SMARTAI_DATABASE_URL_LIGHT="sqlite:///$QA_DIR/test.db" \
 SMARTAI_STORAGE_ROOT="$QA_DIR/uploads" SMARTAI_SEED_TEST_USERS=false SMARTAI_ALLOW_DEMO_TOKENS=true \
 SMARTAI_JWT_SECRET=local-draft-QA-placeholder-0123456789abcdef \
 SMARTAI_PROVIDER_ENCRYPTION_KEY=local-draft-QA-encryption-0123456789abcdef \
 FRONTEND_URLS=http://127.0.0.1:53971 \
 python -m uvicorn qa_backend:app --host 127.0.0.1 --port 8037
```

另一个终端从 `frontend/app` 启动本地 Vite，再执行以下验收；脚本拒绝非 loopback 地址。可用 `DRAFT_QA_APP_URL`、`DRAFT_QA_API_URL`、`DRAFT_QA_OUTPUT` 指定其他隔离端口和证据目录。core/uploaded 创建隔离样本任务；pages/dialogs 使用 GET 合成业务 fixture 并拦截所有业务写入。uploaded 使用假 BYOK 占位串并拦截模型启动。native 中 Firefox 明确允许 beforeunload；不测试真实外部 provider。

```sh
npm ci
npx playwright install chromium firefox webkit
VITE_SMARTAI_BACKEND_URL=http://127.0.0.1:8037 npm run dev -- --host 127.0.0.1 --port 53971 --strictPort
# 另一个终端，cwd=frontend/app
node scripts/drafts/verify-core.mjs
node scripts/drafts/verify-pages.mjs
node scripts/drafts/verify-dialogs.mjs
node scripts/drafts/verify-edges.mjs
node scripts/drafts/verify-uploaded.mjs
npm test
npm run lint
npm run build
```

自动化测试涵盖原快照不自动改写、真实文件、owner/task、版本/过期/损坏、配额、原子回滚、迟到/删除/登出、多标签 CAS、三选项/原目标/重复点击、保存期间编辑、原生注册移除、秘密无存储、初始化恢复及正式字段成功/失败生命周期。截图/JSON 由上述脚本生成，不复制用户原件、数据库或密钥入 PR。

## 平行 PR 与交叉文件

交付前组合基线 main `c93295c595eaefa48adaedacd6816aed8d464e8b`，已保留 #124 题目确认/连续版本、#125 设置门禁、#126 身份/作答确认及保存锁、#127 移动矩阵、#130 PDF 内部连续预览、#131 认证入口。只适配外层输入，不改 PDF 内部、不变更登录协议。#128 尚独立，结果复核保存继续携带原 `confirm` 与 `expected_workflow_revision`，局部离开保存确认逻辑替换为本地暂存，不让导航隐式确认。

#117 私有管理入口尚未在 main 启用。提供可审阅的 [PR117_INTEGRATION.patch](PR117_INTEGRATION.patch)，针对已核对 head `d5141783bde6157ffa34bd7cdbb9dae5ce7a4c8d` 和本 PR 共享模块的组合：私有 root/provider、授权后 owner context、管理员业务设置/账号/用户操作/维护确认及相关测试。保持原 expected_version/global_version、幂等、审计原因、私有 token/role、业务锁；销户/封禁确认及密码不持久化。补丁已做隔离组合类型/构建/38 项测试与 Chromium 私有入口 5 项图形验收（GET fixture，管理员写入拦截）。维护确认/密码只提示离开，不持久化，也不改变清空契约。它是管理员 agent 合入其入口时的交叉文件适配，**不表示本 PR 已开启/部署管理端**。若 #117 变更基线需按新契约重核，不能把补丁当自动上线。

未启用 teacher/student/LMS/旧管理员路由的输入逐项列在清单中；本 PR 不私自打开它们。纯展示、搜索筛选、即时正式保存不制造独立草稿。覆盖状态以清单为准，不用“全站完成”代替尚未启用的页面或浏览器限制。


## 本轮交付验证截点

- 前端 100 文件 / **584 测试通过**；范围审计（107 可见文件）、TypeScript、生产构建、`git diff --check` 通过。
- 相关后端 **68 测试通过**，包括题目/教材只读引用、认证入口和批改门禁；临时 SQLite/文件目录，未使用生产数据。
- 公共浏览器 **85 项通过**：Chromium / Firefox / WebKit 各 7 核心、11 逐页、4 弹窗、5 边界检查，另 4 项真实服务器上传引用验收。桌面 1440×1000、手机 390×844，长表单、键盘 Tab/Shift-Tab/Enter/Escape、重复进入、前进后退、原生刷新/关标签/跨文档离开均按脚本实际触发验证；没有据此保证系统强杀等不受浏览器支持的路径。
- 稿件字节校验、原子存储失败、配额、损坏/旧版/过期、账号/任务、服务端版本变化、保存期间继续修改、迟到响应/删除、多标签竞争由自动化覆盖。新增编程代码/隐藏测试和教材预检手动重试验收；后者首次预检一次、两次显式启动请求，恢复只 GET，失效后原真实文件仍在。
- 管理端 **38 测试 / 5 Chromium 验收**只属于上述精确 #117 组合补丁，不代表 main 已启用入口。现有私有构建保留 >500kB chunk 提示，未扩展为构建重构。

管理员组合重现：在 #117 指定 head 与本 PR 共享模块的隔离组合目录应用补丁，执行 `npm run typecheck`、对应管理员/共享 hook 测试、`VITE_SMARTAI_BACKEND_URL=http://127.0.0.1:8037 npm run build -- --mode admin`；将 `dist-admin` 用仅回环地址的静态服务提供，并对 SPA 路径回退 `admin.html`。随后从本 PR `frontend/app` 执行 `DRAFT_QA_ADMIN_URL=http://127.0.0.1:53972 node scripts/drafts/verify-admin.mjs`。该脚本仅合成 GET fixture，拦截全部管理写入，包括维护操作。
