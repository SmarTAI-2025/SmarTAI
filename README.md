## 环境配置

```
conda create -n env_name python=3.12
conda activate env_name
pip install -r requirements.txt
```

## 运行测试

`cd /path/to/project-root`

先运行后端代码：`python -m uvicorn backend.main:app --reload --host 0.0.0.0 --port 8000`

> --reload 方便开发调试
> 可以省略 python -m

> (可选)在新终端中测试后端：
>
> ```
> curl -X POST "http://localhost:8000/file_preview" \
>      -F "file=@hw.zip"
> ```

再在新终端中运行前端代码：`streamlit run frontend/main.py --client.showSidebarNavigation=False`

> - --client.showSidebarNavigation=False 隐藏 streamlit 默认文件目录导航侧边栏
> - 端口将随机分配，启动后会在控制台显示访问地址
> - --server.headless true：在无头环境（容器、远程服务器）下不自动尝试打开浏览器。开发时本地也可以省略该参数以自动打开浏览器。

## 一体化启动（推荐开发使用）

`streamlit run app.py`

这个脚本会自动启动后端和前端服务，并处理端口分配和环境变量设置。

## AI自动批改功能

本项目新增了AI自动批改功能，支持以下题型：
- 计算题
- 概念题
- 证明题
- 编程题

### 功能说明

1. 学生上传作业后，系统会自动识别题目和答案
2. 点击"开启AI批改"按钮，系统会为每个学生生成批改任务
3. 批改结果会显示在"批改结果"页面

### API接口

- `POST /ai_grading_new/grade_student/` - 启动学生作业批改任务
- `GET /ai_grading_new/grade_result/{job_id}` - 获取批改结果

## 部署指南

详细部署说明请参考 [DEPLOYMENT.md](DEPLOYMENT.md) 文件，其中包含：

1. **托管平台部署**（推荐）：
   - 前端部署到 Streamlit Community Cloud
   - 后端部署到 Render (配置文件位于 [backend/render.yaml](file:///d%3A/work/SmarTAI/backend/render.yaml))

2. **容器化部署**：
   - 使用 Docker 和 Docker Compose
   - 适合需要更多控制的生产环境

3. **环境变量配置**：
   - BACKEND_URL：前端连接后端的URL
   - FRONTEND_URLS：后端允许的前端来源（CORS配置）