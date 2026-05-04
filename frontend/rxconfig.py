"""Reflex application configuration.

Reflex 启动时读这个文件，决定：前端跑哪个端口、Reflex 自己的后端跑哪个端口、
Next.js 前端打到哪里去找 Reflex 后端、要不要启用 Tailwind、要不要在页脚显示 "Built with Reflex"。

注意三层后端区分（容易混淆）：
  1. SmarTAI FastAPI 后端（端口 8000）— 业务接口（/ai_grading 等）
  2. Reflex 自己的 WebSocket 后端（端口 8001）— Reflex 内部用来同步 State
  3. Next.js 前端（端口 3000）— 浏览器加载的页面

本文件仅配置 (2) 和 (3)。SmarTAI 业务后端的地址在 smartai_v2/config.py 的 BACKEND_URL。
"""
import os
import reflex as rx

# Reflex 0.7+ 默认会启用 SitemapPlugin（自动生成 sitemap.xml 用于 SEO）。
# 若不显式声明，启动时会有一行警告。我们暂时不需要 sitemap，显式禁用以静默警告；
# 上线前如要做 SEO，把下面的 disable 改成 plugins=[SitemapPlugin()] 即可。
_extra_kwargs: dict = {}
try:
    from reflex.plugins.sitemap import SitemapPlugin  # type: ignore
    _extra_kwargs["disable_plugins"] = [SitemapPlugin]
except ImportError:
    pass

config = rx.Config(
    # 应用名：必须与包目录名（smartai_v2/）一致，Reflex 据此找入口模块
    app_name="smartai_v2",

    # Next.js 前端端口（浏览器访问的端口）
    # 本地：3000；线上：托管平台分配的端口（Render 会用 PORT 环境变量自动覆盖）
    frontend_port=int(os.environ.get("PORT", 3000)),

    # Reflex WebSocket 后端端口（State 同步用，与 SmarTAI 业务后端不是一回事）
    # 本地:8001（避开 SmarTAI 业务后端的 8000）
    backend_port=int(os.environ.get("REFLEX_BACKEND_PORT", 8001)),

    # Next.js 前端编译出来后，浏览器代码里写死要打到哪里去找 Reflex 后端
    # 本地：http://localhost:8001
    # 线上：必须改成你 Reflex 后端实际部署的公网地址（如 https://smartai-rx.onrender.com）
    api_url=os.environ.get("REFLEX_API_URL", "http://localhost:8001"),

    # 是否启用 Tailwind CSS 集成。我们用 Radix UI（rx.theme），不需要 Tailwind
    tailwind=None,

    # 是否在页脚显示 "Built with Reflex" 推广链接。生产环境关掉
    show_built_with_reflex=False,

    **_extra_kwargs,
)
