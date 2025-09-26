# SmarTAI 部署指南

本指南将帮助您将 SmarTAI 应用的前端 (Streamlit) 和后端 (FastAPI) 分别部署到不同的托管平台。

## 部署架构概述

SmarTAI 应用采用前后端分离架构：

- **前端**: Streamlit 应用，提供用户界面
- **后端**: FastAPI 应用，提供 API 服务

## 部署方案

### 方案一：托管平台部署（推荐）

这是最简单的部署方式，适合快速原型验证和个人项目：

| 组件 | 推荐平台 | 说明 |
|------|----------|------|
| 前端 | [Streamlit Community Cloud](https://share.streamlit.io/) | 官方提供，免费，完美集成 |
| 后端 | [Render](https://render.com/) | 有免费套餐，对Python Web服务支持友好 |

### 方案二：容器化部署

适合需要更多控制和定制的项目：

- 使用 Docker 容器化应用
- 部署到云服务器（如 AWS, Google Cloud, Azure）

## 方案一：托管平台部署步骤

### 步骤 1: 准备代码库

确保您的项目已经使用 Git 管理，并上传到了 GitHub。

项目结构应该如下：

```
SmarTAI/
├── .gitignore
├── README.md
├── requirements.txt
├── DEPLOYMENT.md
├── backend/
│   ├── main.py
│   ├── routers/
│   ├── dependencies.py
│   ├── requirements.txt
│   ├── render.yaml
│   ├── start.py
│   └── ...
├── frontend/
│   ├── main.py
│   ├── pages/
│   ├── utils.py
│   └── ...
└── ...
```

### 步骤 2: 部署后端到 Render

1. 访问 [render.com](https://render.com) 并注册/登录
2. 点击 "New +" -> "Web Service"
3. 连接您的 GitHub 仓库
4. 配置部署设置：
   - **Name**: smartai-backend (或其他您喜欢的名字)
   - **Root Directory**: backend
   - **Environment**: Python 3
   - **Region**: 选择一个离您用户近的区域
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `python start.py`
5. 添加环境变量（在 Dashboard 的 "Environment" 部分）：
   - **Key**: `FRONTEND_URLS`
   - **Value**: `https://smartai-2025-smartai-frontendapp-iie6tb.streamlit.app,http://localhost:8501`
6. 点击 "Create Web Service"
7. 部署成功后，您会得到一个公开的 URL，例如：`https://smartai-backend-zefh.onrender.com`

### 步骤 3: 部署前端到 Streamlit Community Cloud

1. 访问 [share.streamlit.io](https://share.streamlit.io) 并授权您的 GitHub 账号
2. 点击 "New app"，选择您的 GitHub 仓库
3. 配置部署设置：
   - **Repository**: 选择您的项目仓库
   - **Branch**: main
   - **Main file path**: frontend/app.py
4. 点击 "Advanced settings..."
5. 在 "Secrets" 部分，添加后端 URL：
   - **Secret name**: BACKEND_URL
   - **Secret value**: `https://smartai-backend-zefh.onrender.com` (使用您在步骤2中获得的URL)
6. 点击 "Deploy!"

## 方案二：容器化部署

如果您需要更多控制，可以使用 Docker 容器化应用。

### Docker 配置

创建 `backend/Dockerfile`:

```dockerfile
FROM python:3.9-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY backend/ ./backend
COPY *.json ./  # Copy config files

EXPOSE 8000

CMD ["python", "backend/start.py"]
```

创建 `frontend/Dockerfile`:

```dockerfile
FROM python:3.9-slim

WORKDIR /app

COPY frontend/requirements.txt .
RUN pip install -r frontend/requirements.txt

COPY . .

EXPOSE 8501

CMD ["streamlit", "run", "frontend/app.py", "--server.port=8501", "--server.address=0.0.0.0"]
```

### 使用 Docker Compose

创建 `docker-compose.yml`:

```yaml
version: '3.8'

services:
  backend:
    build:
      context: .
      dockerfile: backend/Dockerfile
    ports:
      - "8000:8000"
    environment:
      - BACKEND_PORT=8000
      - FRONTEND_URLS=https://smartai-2025-smartai-frontendapp-iie6tb.streamlit.app,http://localhost:8501

  frontend:
    build:
      context: .
      dockerfile: frontend/Dockerfile
    ports:
      - "8501:8501"
    environment:
      - BACKEND_URL=http://backend:8000
    depends_on:
      - backend
```

运行应用：

```bash
docker-compose up --build
```

## 环境变量配置

应用使用以下环境变量：

### 后端 (FastAPI)

- `FRONTEND_URLS`: 允许访问后端的前端URL列表，用逗号分隔（默认："http://localhost:8501"）
- `BACKEND_PORT`: 后端服务端口（本地开发时使用）

### 前端 (Streamlit)

- `BACKEND_URL`: 后端服务的URL（默认："http://localhost:8000"）

## 本地开发

在本地运行应用：

### 运行后端

```bash
cd backend
python start.py
```

或者使用 uvicorn directly:

```bash
cd backend
uvicorn main:app --reload --host localhost --port 8000
```

### 运行前端

```bash
streamlit run frontend/app.py
```

或者使用一体化启动脚本：

```bash
python frontend/app_cloud.py
```

## 故障排除

### CORS 错误

如果遇到跨域问题，请检查后端的 `FRONTEND_URLS` 环境变量是否包含了前端的URL。

### 连接问题

确保前端能够访问后端URL。在部署环境中，使用实际的域名而不是 localhost。

### 环境变量未生效

在 Streamlit Community Cloud 中，确保在 "Advanced settings" 的 "Secrets" 部分正确设置了环境变量。

### 依赖安装问题

如果在 Render 上部署时遇到依赖安装问题，可能是因为依赖冲突。我们已经将依赖声明简化为最小化版本，只保留必要的包名而没有版本约束，以允许 pip 自动解决依赖关系。

### 模块导入问题

如果遇到 `ModuleNotFoundError` 错误，这是因为 Python 路径没有正确设置。我们通过以下方式解决：
1. 在 [backend/main.py](file:///d%3A/work/SmarTAI/backend/main.py) 中添加了项目根目录到 Python 路径
2. 创建了 [backend/start.py](file:///d%3A/work/SmarTAI/backend/start.py) 启动脚本，确保正确的路径设置
3. 在 [backend/render.yaml](file:///d%3A/work/SmarTAI/backend/render.yaml) 中使用启动脚本而不是直接调用 uvicorn

### 后端根路径返回 {"detail":"Not Found"}

这是正常的，因为我们没有为根路径 [/](file://d:\work\SmarTAI\app_cloud.py) 定义处理函数。现在我们已经添加了一个根路径处理函数，它会返回一个友好的消息，表明后端正在运行。

### Streamlit Health Check 失败

如果遇到 "connection refused" 错误，请检查以下几点：

1. 确保 [frontend/.streamlit/config.toml](file:///d%3A/work/SmarTAI/frontend/.streamlit/config.toml) 文件中的配置正确：
   ```toml
   [server]
   port = 8501
   address = "0.0.0.0"
   headless = true
   ```

2. 确保 Streamlit 应用能够正确启动，检查 [frontend/app.py](file:///d%3A/work/SmarTAI/frontend/app.py) 或 [frontend/app_cloud.py](file:///d%3A/work/SmarTAI/frontend/app_cloud.py) 文件

3. 在 Streamlit Community Cloud 中，确保 Main file path 设置为 `frontend/app.py`