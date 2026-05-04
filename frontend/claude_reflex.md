1. 每个参数的含义（关键是搞清楚"三层后端"）
很多人初学 Reflex 都被 3 个端口绕晕——必须先理清楚：

角色	|默认端口	|谁运行	|干什么用
Next.js 前端	3000	浏览器加载	用户实际看到的网页
Reflex 自己的 WebSocket 后端	8001	Python 进程	仅用于同步 State（你写的 Python 事件处理函数跑在这里）
SmarTAI FastAPI 业务后端	8000	Python 进程	真正的业务接口（/ai_grading 等）
rxconfig.py 只配置前两个，第三个由 smartai_v2/config.py 的 BACKEND_URL 配置。

数据流：用户点按钮 → Next.js 通过 WebSocket 把事件发到 Reflex 后端 → Reflex 后端运行你的 Python 事件函数 → 该函数用 httpx 调 SmarTAI 业务后端 → 结果回 State → WebSocket 推回浏览器 → Next.js 自动重渲染。

参数逐项
参数	含义	本地值	公网值
app_name	Reflex 据此找入口模块，必须等于包目录名（smartai_v2/）	"smartai_v2"	不变
frontend_port	Next.js 前端端口（浏览器访问）	3000	Render/Vercel 会自动设 PORT 环境变量覆盖
backend_port	Reflex WebSocket 后端端口	8001	同上
api_url	Next.js 编译时打到代码里的"Reflex 后端地址"，浏览器据此找 WebSocket	http://localhost:8001	必须改成公网 https URL，如 https://smartai-rx.onrender.com
tailwind	是否启 Tailwind。我们用 Radix UI（rx.theme），不需要	None	不变
show_built_with_reflex	是否显示推广链接	False	不变
api_url 的坑：它是构建时写死到前端静态资源里的，不是运行时读环境变量。所以部署前必须用环境变量在构建脚本里设好，否则浏览器会去 localhost:8001 找 WebSocket（永远连不上）。

2. 本地测试流程

# 终端 1：启动 SmarTAI FastAPI 业务后端
conda activate smartai
python -m uvicorn backend.main:app --reload --host 0.0.0.0 --port 8000

# 终端 2：启动 Reflex（一条命令同时启 :3000 前端 + :8001 Reflex 后端）
cd frontend_v2
pip install -r requirements.txt
reflex init     # 仅首次，初始化 .web/ 目录
reflex run      # 后续直接用这个

# 浏览器访问 http://localhost:3000
调试时也可以加旧 Streamlit 同时跑（端口 8501），三者互不冲突。

3. 公网部署：参数改动
推荐方案：Render 部署 3 个服务
部署架构：


浏览器
  │
  ├─ HTTPS → smartai-rx.onrender.com         (Reflex 应用：Next.js + Reflex 后端打包)
  │            │
  │            └─ HTTPS → smartai-backend-xxx.onrender.com   (SmarTAI FastAPI 业务后端)
改动 1：rxconfig.py 改成读环境变量（这样本地/公网都能用同一份代码）：


import os
import reflex as rx

config = rx.Config(
    app_name="smartai_v2",
    frontend_port=int(os.environ.get("PORT", 3000)),
    backend_port=int(os.environ.get("REFLEX_BACKEND_PORT", 8001)),
    api_url=os.environ.get("REFLEX_API_URL", "http://localhost:8001"),
    tailwind=None,
    show_built_with_reflex=False,
)
改动 2：smartai_v2/config.py 的 SmarTAI 业务后端 URL（已经写成读环境变量了，无需改）：


BACKEND_URL = os.environ.get("SMARTAI_BACKEND_URL", "http://localhost:8000")
改动 3：在 Render 后台为 Reflex 服务配置环境变量：

变量	值
REFLEX_API_URL	https://smartai-rx.onrender.com（你 Reflex 服务的公网域名）
SMARTAI_BACKEND_URL	https://smartai-backend-xxx.onrender.com（你 SmarTAI 后端的公网域名）
改动 4：SmarTAI 后端的 CORS 白名单（在 Render 后台修改 SmarTAI 后端服务的环境变量）：


FRONTEND_URLS=https://smartai-rx.onrender.com,http://localhost:3000
改动 5：Render 部署 Reflex 时的启动命令（Render 后台 Web Service 的 startCommand）：


REFLEX_API_URL=$REFLEX_API_URL reflex run --env prod
或更稳的 Docker 部署方式——后续 Stage 5 上线时我会写一个 Dockerfile 给你。

自定义域名（可选）
如果你想用 smartai.edu.cn 而不是 xxx.onrender.com：

阿里云/腾讯云买域名（约 ¥50-100/年）
Render 服务设置里绑定自定义域名 → Render 给你一个 CNAME
在域名 DNS 配置里把 app.smartai.edu.cn CNAME 到 Render 提供的地址
Render 自动签 Let's Encrypt SSL 证书
更新 REFLEX_API_URL 和 FRONTEND_URLS 为新域名
4. 三种部署方案对比
方案	月成本	国内可访问性	配置难度
Render（推荐）	免费档够用，付费 $7/月起	需梯子（节点在美国/欧洲）	低
Reflex Cloud	免费档/付费	同上	极低（一行 reflex deploy）
腾讯云轻量服务器（国内访问最佳）	¥24/月起	国内直连	中（需手写 Dockerfile + Nginx）
如果你的目标用户在国内，强烈建议腾讯云轻量 + 域名备案——速度快太多。

现在 rxconfig.py 已写好（含详细注释）、配置常量、主题、README、设计文档、后端接口契约都已落盘。需要我继续往下搭吗？接下来是：API 客户端层（httpx）→ State 类 → 共享组件（Layout/Sidebar/Topbar）→ 第一个真页面（登录）。

我可以一气呵成全部写完（约 30+ 文件），或你想我每写完一个模块就停下来给你讲解一次（更慢但你能跟上每一步的设计）？