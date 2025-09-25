# app/main.py
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from backend.routers import prob_preview, hw_preview, ai_grading, human_edit
# from app.db import init_db
import logging

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

    # 允许所有来源的跨域请求，便于本地开发
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

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
#                 q_id = problem.get('id')
#                 if q_id:
#                     # Store the problem data with q_id as the key
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
    logger.info("启动FastAPI后端服务，监听 http://127.0.0.1:8000")
    uvicorn.run(app, host="127.0.0.1", port=8000)
