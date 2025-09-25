import os
import io
import logging
import zipfile
import rarfile
import py7zr
import tarfile
import json
import asyncio
from typing import List, Dict, Any
from fastapi import APIRouter, Depends, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from langchain_openai import ChatOpenAI
# from langchain_core.pydantic_v1 import BaseModel, Field

from pydantic import BaseModel, Field
from langchain_core.messages import SystemMessage, HumanMessage
from langchain.schema.document import Document
from langchain.text_splitter import RecursiveCharacterTextSplitter
from langchain.chains.summarize import load_summarize_chain

# from ..dependencies import get_problem_store, get_llm, ProblemSet
from ..dependencies import *
from ..utils import *

# --- 日志和应用基础设置 ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/prob_preview",
    tags=["prob_preview"]
)

SYSTEM_PROMPT = """
你是一个专业的AI助教，拥有相关领域的研究生专业知识水平，专门负责分析纯文本格式的作业内容。你的任务是：
1.  **题目分割**：将识别出的内容分割成独立的题目。根据题号（如 "1.1", "第二题", "III." 等）来区分。
2.  **内容提取**：为每道题目提取三个关键信息：
    - `number`: 题号。
    - `stem`: 题目完整的题干内容，包括所有文字、公式和代码块。
3.  **题目分类**：根据以下定义，为每道题确定一个最合适的分类 (`type`)：
    - **概念题**: 答案基本确定或含义相近即可以判定正确与否或者正确百分比。
    - **计算题**: 需要进行数值计算或符号计算以准确验证。
    - **编程题**: 包含代码片段，或要求编写代码。
    - **证明题**: 要求从已知条件出发，通过逻辑推导得出题干已提供的结论。
    - **推理题**: 要求根据已有信息进行逻辑推理，得出题干未提供的结论或判断。
    - **其他**: 无法归为以上5类的题目。
**[重要指令]：请保证提取题干的完整性，你被禁止自行删减内容，也不允许进行翻译，请“完整”保留题干信息，你的工作只是将他们划分为多个题目。**
4. **设计题目评分标准细则（`criterion`）**: 如果题目中明确给出了评分细则，或者教师单独提交了评分细则，如 "满分10分..."、“每个错误扣2分...”，则保留。如果没有，则此字段需要你根据题目类型以及批改严格程度自行设计；教师或题目提交的批改细则不够细致到支撑给每个题目严谨打分时，也需要你设计并补充。

5.  **格式化输出**：将所有处理好的题目整合成一个字典，key为\"problems\"，value为一个JSON数组，数组每个元素是一个JSON对象字典，包含 "q_id", "number", "type", "stem", "criterion" 这几个字段。
例如，对于一个包含5道题的图片，你的输出应该是这样的结构：
{"problems":
    [
        {"q_id": "q1", "number": "1.1", "type": "概念题", "stem": "请解释什么是“依赖注入”？", "criterion": "满分10分，答错全扣分，答对满分。"},
        {"q_id": "q2", "number": "1.2", "type": "计算题", "stem": "求解方程 $x^2 - 5x + 6 = 0$。", "criterion": "满分10分，两个结果每个2分，计算过程6分"},
        {"q_id": "q3", "number": "2", "type": "编程题", "stem": "使用Python编写一个快速排序算法。", "criterion": "满分10分，6个测试样例每通过一个1分，是快速排序算法4分。"},
        {"q_id": "q4", "number": "3.1", "type": "证明题", "stem": "证明三角形内角之和为180度", "criterion": "每步推导1分，正确证明了结论2分。"},
        {"q_id": "q5", "number": "3.2", "type": "推理题", "stem": "向上抛出一个小球，测出小球抛出后两次经过某竖直位置$A$的时间间隔$T_A$和经过另一竖直位置$B$的时间间隔$T_B$。若已知$B$在$A$上方$h$处，试求重力加速度$g$", "criterion": "每步推导1分，正确推理出了结论4分。"}
    ]
}
**[重要指令]:你的回答输出必须严格按照上述格式，必须直接以 `{` 字符开始，并以 `}` 字符结束。禁止在JSON对象前后添加任何前沿、导语、解释、注释、代码或任何非JSON内容，也不要包含换行符、tab符等。**
**[务必注意]:在生成JSON时，请确保字符串值中的所有反斜杠 \"\\" 都被正确转义为 \"\\\"。这对于包含LaTeX公式的字段尤其重要。***
"""

def process_and_store_problems(
    text: str,
    llm: Any, # 接收LLM客户端实例
    problem_store: Dict[str, Any] # 接收题目存储字典的引用
) -> Dict[str, Any]:
    """
    接收文本，调用AI处理，并将结果存入指定的存储中。
    这是一个可复用的业务逻辑函数。
    """
    if not text or not text.strip():
        raise HTTPException(status_code=400, detail="输入的文本为空或只包含空白字符。")

    try:
        # structured_llm = llm.with_structured_output(ProblemSet)
        messages = [
            SystemMessage(content=SYSTEM_PROMPT),
            HumanMessage(content=text)
        ]
        
        print("正在调用AI分析题目...")
        # response_obj: ProblemSet = await structured_llm.ainvoke(messages)

        raw_llm_output = llm.invoke(messages).content
        json_output = parse_llm_json_output(raw_llm_output, ProblemSet)
        print("AI分析完成。")
        
        if not (json_output and json_output.problems):
            raise HTTPException(status_code=500, detail="AI未能从文本中提取出任何有效的题目。")

        # 直接dump整个对象
        new_problem_data = json_output.model_dump()
        
        prob_dict = {q['q_id']: q for q in new_problem_data.get('problems', [])} #以q_id为key索引

        # 修改传入的字典对象
        problem_store.clear()
        problem_store.update(prob_dict)
        
        # 返回处理后的结果，以便API端点可以将其作为响应返回
        return prob_dict

    except Exception as e:
        # 将底层错误包装成HTTPException
        raise HTTPException(status_code=500, detail=f"AI处理过程中发生错误: {e}")

@router.post("/")
async def handle_homework_upload(
    file: UploadFile = File(...),
    # 像其他端点一样，注入所需要的依赖
    problem_store: Dict = Depends(get_problem_store),
    llm: Any = Depends(get_llm)
):
    """
    接收上传的作业文件，解码后交由服务函数处理，并返回分析结果。
    """
    logger.info(f"接收到文件: {file.filename}, 类型: {file.content_type}")
    
    try:
        # 1. 处理文件 I/O 和解码
        text_bytes = await file.read()
        text = decode_text_bytes(text_bytes)
        logger.info(f"文件内容: {text}")
        
        # 2. 调用核心服务函数处理业务逻辑
        # 将解码后的文本和注入的依赖传递给服务函数
        # recognized_hw = await process_and_store_problems(
        #     text=text,
        #     llm=llm,
        #     problem_store=problem_store
        # )
        
        recognized_hw = await asyncio.to_thread(
            process_and_store_problems,
            text=text,
            llm=llm,
            problem_store=problem_store
        )
        logger.info(f"识别并存储了 {len(recognized_hw)} 个题目。")
        logger.info(f"识别题目内容: {recognized_hw}")
        
        # 3. 返回成功响应
        return JSONResponse(content=recognized_hw)

    except HTTPException as e:
        # 直接抛出由服务函数或解码函数生成的HTTPException
        logger.error(f"HTTP错误: {e.detail}")
        raise e
    except Exception as e:
        logger.exception(f"处理文件 '{file.filename}' 时发生未知错误。")
        raise HTTPException(status_code=500, detail=f"服务器内部错误: {e}")