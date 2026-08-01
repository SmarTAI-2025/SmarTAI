# SmarTAI

> SmarTAI 是一款面向高校理工科课程的 AI 作业批改平台。围绕课程、作业和学生作答组织批改流程，可在几分钟内完成初评，并通过多 AI 协同评审，让结果可追溯、可复核、可发布。

## 在线 Demo

🔗 **在线体验**：[https://smartai-course.pages.dev](https://smartai-course.pages.dev/)

> 当前在线 Demo 尚未同步到仓库最新版，功能和界面可能与最新代码有所不同。

🔗 **测试账号**：等待邀请发放

---

## 本地运行（开发与测试）

### 当前代码状态

项目主要由三部分组成：

- **FastAPI 后端**：位于 `backend/`，提供认证、课程、作业、提交、批改、成绩、知识库和模型配置等 API。
- **Vite React 前端**：位于 `frontend/app/`，分别为管理员、教师和学生提供对应的工作区。
- **持久化层**：使用 SQLAlchemy + Alembic 管理 SQLite / PostgreSQL，并通过本地文件系统或 S3 兼容对象存储保存上传文件。

### 环境分工

- **后端使用 Python 3.11 或 3.12**：FastAPI、LLM、RAG、SymPy 和数据库迁移等依赖均在 Python 环境中运行。
- **React 前端使用 Node.js 20 + npm**：依赖版本由 `frontend/app/package-lock.json` 锁定，无需安装 Python 前端包。
- **本地默认使用轻量模式**：SQLite 数据库位于 `data/smartai.db`，上传文件保存在 `data/uploads/`。

### 准备后端 Python 环境

可以使用 Conda 创建并激活专用环境：

```bash
conda create -n smartai python=3.11
conda activate smartai
```

也可以使用标准虚拟环境：

```bash
python -m venv .venv
```

环境激活后请统一使用 `python` / `pip`，避免与系统 Python 混淆。

### 安装后端依赖

在仓库根目录执行：

```bash
python -m pip install --upgrade pip
pip install -r render-requirements.txt
```

从示例创建本地配置：

```bash
cp .env.example .env
```

Windows PowerShell 使用：

```powershell
Copy-Item .env.example .env
```

#### 配置本地 BYOK 加密主密钥

`.env.example` 中的 `SMARTAI_PROVIDER_ENCRYPTION_KEY` 和 `SMARTAI_JWT_SECRET`
只是公开占位值，不能直接使用。先运行下面的命令**两次**，分别生成两个至少 32 字节、
互不相同的随机值：

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

把两次输出分别填入本地 `.env`：

```dotenv
SMARTAI_PROVIDER_ENCRYPTION_KEY=在这里粘贴第一个随机值
SMARTAI_JWT_SECRET=在这里粘贴第二个随机值
```

- `.env` 已被 Git 忽略，不要提交、上传或通过普通聊天发送其中的值。
- `SMARTAI_PROVIDER_ENCRYPTION_KEY` 是服务器用于加密用户 BYOK 的主密钥，不是
  Gemini、OpenAI 等服务商的 API key，也不要与 JWT 签名密钥共用。
- 一旦保存过 BYOK，就必须在后续重启中继续使用同一个值；丢失或更换它会使已有密文
  无法解密。连接同一个共享数据库的后端实例必须从安全的 Secret Manager 读取同一主密钥。
- 新建且互不共享的本地数据库可以各自生成独立主密钥。

首次运行前，在**仓库根目录**（该目录应能看到 `alembic.ini`、`backend/` 和
`frontend/`）应用数据库迁移。请先激活上文创建并已安装后端依赖的 Python 环境，
例如 Conda `smartai` 或项目 `.venv`；不要在 `frontend/app/` 或未安装依赖的系统
Python 环境中执行。

```bash
cd /path/to/SmarTAI
conda activate smartai  # 使用 .venv 时改为 source .venv/bin/activate
python -m alembic upgrade head
```

Windows PowerShell 使用 `.venv` 时，激活命令为：

```powershell
cd C:\path\to\SmarTAI
.\.venv\Scripts\Activate.ps1
python -m alembic upgrade head
```

看到 `Running upgrade ...` 或数据库已经处于 `head` 即表示完成。以后拉取包含新
migration 的代码后，也应在启动后端前从仓库根目录重新执行同一命令；正常升级不会
要求从其他合作者处获取数据库文件。

### 创建第一个管理员

系统默认关闭公开注册。首次启动前，请在仓库根目录创建管理员：

```bash
python scripts/create_admin.py admin --email admin@example.com
```

命令会以交互方式读取至少 10 个字符的密码。管理员登录后可创建用户或发放邀请码。

如需一批账号进行本地联调，可以生成一份已被 Git 忽略的测试凭据文件：

```bash
python scripts/generate_test_users.py
```

### 启动后端

在仓库根目录执行：

```bash
python -m uvicorn backend.main:app --reload --host 0.0.0.0 --port 8000
```

后端服务监听 `8000` 端口：

- 健康检查：`http://localhost:8000/health`
- 就绪检查：`http://localhost:8000/ready`
- OpenAPI 文档：`http://localhost:8000/docs`

### 启动 React 前端

在另一终端中执行：

```bash
cd frontend/app
npm ci
npm run dev
```

浏览器打开：

```text
http://localhost:5173
```

React 前端默认连接 `http://localhost:8000`。如需连接其他后端，请在 `frontend/app/.env` 中设置：

```dotenv
VITE_SMARTAI_BACKEND_URL=http://localhost:8000
```

修改配置后，需要重启 `npm run dev` 或重新构建前端。

### 运行测试

后端测试（在仓库根目录执行）：

```bash
pip install pytest pytest-asyncio
python -m pytest backend/tests -q
```

前端检查（在 `frontend/app/` 执行）：

```bash
npm run audit:scope
npm run typecheck
npm test
npm run build
```

---

## 部署到公网

仓库已提供后端的 Render 部署配置，推荐使用以下组合：

- **后端**：Render Web Service，配置文件为 `backend/render.yaml`
- **前端**：Cloudflare Pages，构建根目录为 `frontend/app`
- **数据库**：PostgreSQL
- **文件存储**：S3 兼容对象存储

后端在生产环境启动时会先执行数据库迁移，再启动 FastAPI：

```bash
alembic upgrade head && uvicorn backend.main:app --host 0.0.0.0 --port $PORT
```

React 静态前端使用以下构建配置：

```text
Root directory: frontend/app
Build command: npm ci && npm run build
Build output directory: dist
VITE_SMARTAI_BACKEND_URL=https://your-backend.example.com
```

后端生产环境至少需要配置以下项目：

- `SMARTAI_DATABASE_HEAVY=ON` 与 `SMARTAI_DATABASE_URL_HEAVY`
- `SMARTAI_STORAGE_BACKEND=object` 与 `SMARTAI_STORAGE_S3_*`
- `SMARTAI_PROVIDER_ENCRYPTION_KEY` 与 JWT 签名密钥
- `SMARTAI_REFRESH_COOKIE_SECURE=true`
- `SMARTAI_REFRESH_COOKIE_SAMESITE=none`
- `FRONTEND_URLS`（填写真实前端域名，不带结尾 `/`）

`backend/render.yaml` 中 `SMARTAI_PROVIDER_ENCRYPTION_KEY` 的 `sync: false` 只表示该值
必须由部署者提供，Render 不会自动生成。请在后端服务的 Environment / Secret Manager
中设置一个长期稳定的随机值后再部署；连接同一数据库的全部后端实例必须使用同一个值。
不要把该值写入仓库、构建日志或前端环境变量，并在受控的秘密管理系统中保留恢复备份。

生产环境应设置 `SMARTAI_DATABASE_AUTO_CREATE=false`，并统一通过 `alembic upgrade head` 管理数据库结构。部署完成后，可访问 `/ready` 检查数据库和存储是否可用。

---

## 这是什么

SmarTAI 以“建课、布置、提交、批改、复核、发布”为主线，形成完整的作业管理流程：

```text
管理员建账号  →  教师建课 + 布置作业  →  学生提交答案  →  AI 多专家协同评分  →  教师复核 + 发布成绩
  用户/邀请码       题目 + 评分标准         在线作答/文件       独立评分 + 分歧识别        调分/评语/正式发布
```

教师无需编写代码，模型密钥也由各用户自行配置。课程和作业创建完成后，学生可直接提交答案；AI 完成初评后，教师集中处理需要关注的结果，再将确认过的成绩发布给学生。

---

## 特色功能

### 🎯 多 AI 协同评分（核心亮点）

SmarTAI 支持用户**同时配置多家 LLM**（Gemini / OpenAI / Anthropic / 智谱 GLM）。不同模型可对同一份答案独立判断，系统再根据分数差异、置信度和异常状态，标记需要人工复核的结果。

- **真并行调用**：多家 AI 可以同时执行，减少串行等待时间
- **原始判断可追溯**：分别保存 AI 分数、评语和批改状态，完整保留评分过程
- **分歧主动暴露**：低置信度或专家意见存在分歧时，结果会进入人工复核队列
- **单点失败不影响整批任务**：单题失败会记录为待处理结果，教师可以单独查看和处理

### 📐 四种题型，四套批改方法

不同题型对批改方法有不同要求。SmarTAI 会根据题型选择对应的工具和评分流程：

| 题型 | 处理方法 |
| --- | --- |
| **概念题** | 知识检索 + 结构化评分 + 依据说明 |
| **计算题** | SymPy 数值/符号验证 + LLM 步骤评分 |
| **编程题** | Python 沙箱运行测试用例 + LLM 代码评价 |
| **证明题/推理题** | 分步骤分析 + 知识检索 + 长答案处理 |

> 程序能否通过测试、计算结果能否通过验算，优先由确定性工具判断；LLM 主要负责理解解题过程和文字表达，以减少仅凭模型判断带来的偏差。

### 🔑 BYOK（自带 API Key）

用户自行配置 API Key，token 费用由各自的模型账号承担。凭据写入数据库前会使用服务端主密钥加密，接口返回时始终保持脱敏。

- 国内可配置智谱 GLM
- 海外可配置 OpenAI / Gemini / Anthropic
- 至少配置一家模型即可批改；配置多家后可启用多专家协同
- 每个账号只加载自己的 provider 配置，不与其他用户混用

### 👤 全程人工可控

AI 负责辅助批改，最终决定仍由教师作出。以下关键节点均保留人工控制：

- ✏️ 作业发布前修改题目、顺序和评分标准
- ✏️ 学生修正答案时创建不可变的新修订版本，不覆盖历史提交
- ✏️ 教师修改分数、补充评语，并保留 AI 原始结果
- ✏️ 成绩不会自动对学生公开，必须由教师确认并发布

### 📚 课程、作业与三角色工作区

系统按照实际教学关系组织课程、作业、提交和成绩：

- **管理员** —— 管理用户、邀请码和系统状态
- **教师** —— 创建课程、管理学生、发布作业、启动批改、复核并发布成绩
- **学生** —— 查看已选课程和已发布作业，提交答案，只查看正式发布的成绩
- **权限跟随资源归属** —— 课程、作业、提交和成绩均有明确归属，越权访问由后端拒绝

### ⚡ 持久化批改 + 不可变修订

- **批改运行可恢复**：运行状态保存在数据库中，后台工作器通过租约领取任务
- **提交历史不被覆盖**：学生每次更正都会生成新的 revision，批改任务锁定具体版本
- **同一作业避免重复运行**：数据库约束会阻止互相冲突的排队或运行中批改
- **发布结果有明确版本**：学生看到的是教师选择并发布的那次批改结果

### 📡 清晰的批改状态与复核队列

批改过程中会展示清晰的运行状态和处理进度：

- 批改运行区分排队、运行、完成、失败和发布状态
- 教师可以查看已完成数量、结果列表和异常信息
- 低置信度、专家分歧或执行失败的题目集中进入复核队列
- 单题处理完成后，再由教师决定整次成绩何时对学生可见

### 🛡️ 可信的评分质量

系统通过三层机制降低 AI 误判带来的影响：

1. **置信度（confidence）** —— 低于阈值的结果需要人工关注
2. **多专家相互校验** —— 模型意见明显分歧时显式标记，避免用平均分掩盖问题
3. **教师最终确认** —— 分数和评语可复核，发布权始终在教师手中

---

## 适合谁用

- 高校**理工科课程**的任课教师 / 助教 / 课程负责人
- 需要在同一条工作流中管理课程、作业、提交和成绩
- 作业题型涵盖**概念、计算、编程、证明、推理**
- 重视**完整的批改记录**，需要追溯评分过程和结果
- 需要先人工复核，再把正式成绩发布给学生

---

## 一份典型工作流

> 示例场景：30 人完成一份包含 10 道题的“算法分析”作业，题型涵盖概念、计算、编程和证明。

```text
[课前]   管理员创建教师和学生账号，或发放对应角色的邀请码
[10:00] 教师创建课程 → 添加学生 → 新建作业和 10 道题 → 配置评分标准
[10:10] 教师发布作业，学生在自己的课程页查看题目并陆续提交
[截止后] 教师检查提交情况 → 选择模型专家 → 启动一次持久化批改运行
[批改中] 后台逐份处理最新提交修订 → 保存逐题分数、评语、置信度和异常状态
[完成后] 教师打开复核队列 → 调整需要处理的分数和评语
[发布时] 教师发布本次批改 → 学生在“我的成绩”中看到正式结果
```

在整个流程中，学生提交、AI 初评、教师修订和最终发布各自保留清晰边界。每次提交的内容、批改所使用的修订版本以及最终发布的结果，都可以沿数据链路追溯。

---

## 当前能力边界

| 已经能做 | 暂时还不行 |
| --- | --- |
| 管理员 / 教师 / 学生三角色权限 | 多学校、多院系的复杂组织层级 |
| 课程、选课、作业、提交、批改、发布闭环 | 学期排课、考勤等完整教务系统能力 |
| 多 AI 协同 + 人工复核队列 | 完全无人值守地自动发布成绩 |
| 概念 / 计算 / 编程 / 证明题批改工具 | 自动从任意题面生成可靠的编程测试用例 |
| 结构化答案和文件提交 + 不可变修订 | 通用手写图片 OCR 的稳定识别 |
| Python 沙箱 | C / C++ / Java 等多语言沙箱 |
| 逐题统计与教师复核 | 自然语言班级分析和完整成绩导出 |
| SQLite / PostgreSQL + 本地 / 对象存储 | 离线客户端与跨设备本地同步 |

---

## 设计哲学

**SmarTAI 的核心原则是：LLM 评分过程应当可追溯，成绩必须经过教师确认后才能发布。**

- 你能看到每道题的 AI 分数、评语、置信度和处理状态
- 你可以确认每次批改对应学生的哪一版提交，后续修改不会覆盖原有记录
- 你可以修改分数、补充教师评语，并保留原始 AI 判断
- 学生只看到教师已经确认并发布的结果

评估批改类工具时，可以重点关注以下问题：

1. 它能把课程、作业、提交和成绩的关系讲清楚吗？
2. 它能保留 AI 判分依据和学生提交历史吗？
3. AI 评错时，教师能否集中复核并留下修订记录？
4. 成绩是否必须经过教师确认才会对学生可见？

SmarTAI 希望在不削弱教师最终决定权的前提下，减少重复批改所占用的时间。
