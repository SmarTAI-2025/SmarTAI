# app/main.py
import sys
import os

# Add the project root to the Python path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from backend.routers import prob_preview, hw_preview, ai_grading, human_edit
# from app.db import init_db
import logging
import random

# --- 日志和应用基础设置 ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def create_app() -> FastAPI:
    app = FastAPI(title="SmarTAI")

    # 可以在这里做全局中间件、事件、异常处理器注册等
    # include 各模块的 router
    app.include_router(prob_preview.router)   # 会自动挂载到 /file_preview（见 file_preview.py）
    app.include_router(hw_preview.router)   # 会自动挂载到 /file_preview（见 file_preview.py）
    app.include_router(ai_grading.router)   # 挂载到 /ai_grading
    app.include_router(human_edit.router)

    # Configure CORS for deployment
    # For local development, allow all origins
    # For production, you should specify the exact origins
    frontend_origins = os.environ.get("FRONTEND_URLS", "http://localhost:8501,http://localhost:8501")
    origins = frontend_origins.split(",")
    
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/")
    def read_root():
        return {"message": "SmarTAI Backend is running", "status": "success"}

    @app.get("/health")
    async def health_check():
        import psutil
        import os
        
        # Get memory usage
        process = psutil.Process(os.getpid())
        memory_info = process.memory_info()
        memory_mb = memory_info.rss / 1024 / 1024
        
        # Get CPU usage
        cpu_percent = process.cpu_percent()
        
        return {
            "status": "healthy", 
            "message": "SmarTAI backend is running",
            "memory_usage_mb": round(memory_mb, 2),
            "cpu_percent": cpu_percent
        }

    return app

app = create_app()

# Load problem data on startup
# @app.on_event("startup")
# async def load_problem_data_on_startup():
#     """Load problem data from JSON files when the application starts."""
#     try:
#         import os
#         import json
#         from backend.dependencies import problem_data
        
#         # Load problem data from answer_index_by_problems.json
#         problems_file = os.path.join(os.path.dirname(__file__), 'routers', 'answer_index_by_problems.json')
        
#         if os.path.exists(problems_file):
#             with open(problems_file, 'r', encoding='utf-8') as f:
#                 data = json.load(f)
                
#             # Convert to the expected format
#             problems = data.get('students', [])
#             for problem in problems:
#             # Store the problem data with q_id as the key
#                     problem_data[q_id] = {
#                         'q_id': q_id,
#                         'number': problem.get('number', ''),
#                         'type': problem.get('type', '概念题'),
#                         'stem': problem.get('stem', ''),
#                         'criterion': problem.get('criterion', '默认评分标准')
#                     }
            
#             logger.info(f"Loaded {len(problem_data)} problems into problem_data on startup")
#         else:
#             logger.warning(f"Problems file not found: {problems_file}")
            
#     except Exception as e:
#         logger.error(f"Error loading problem data on startup: {e}")

# # 如果需要在启动时初始化 DB 或其它资源
# @app.on_event("startup")
# async def on_startup():
#     init_db()

# --- 本地启动服务器 ---
if __name__ == "__main__":
    import uvicorn
    
    # Get port from environment variable or use random port
    port = int(os.environ.get("BACKEND_PORT", random.randint(8000, 9000)))
    
    logger.info(f"启动FastAPI后端服务，监听 http://localhost:{port}")
    uvicorn.run(app, host="localhost", port=port)  # Changed from 127.0.0.1 to localhost