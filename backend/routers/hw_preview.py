import os
import io
import logging
import json
import asyncio
import concurrent.futures
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


# from ..dependencies import get_problem_store, get_student_store, get_llm, StudentSubmission
from ..dependencies import *
from ..utils import *


# --- 日志和应用基础设置 ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/hw_preview",
    tags=["hw_preview"]
)

# --- 1. 设计 Prompt ---

SYSTEM_PROMPT = """
你是一个专业的AI助教，，拥有相关领域的研究生专业知识水平，专门负责分析纯文本格式的作业解答内容，擅长处理和结构化学生的作业提交。
你的任务是分析单个学生的提交文件，并完成以下两项工作：
1.  **身份识别**: 从提供的【文件名】中，准确提取学生的【学号】和【姓名】。
    - `stu_id`: 学生的学号，通常是字母和数字的组合或者纯数字；若不存在，则填写空字符串。
    - `stu_name`: 学生的姓名，通常是2~4个汉字，或者是包含首字母大写的拼音名或英文名；若不存在，则填写空字符串。
2.  **答案分割**: 从提供的纯文本学生作答中，根据提供的【题目数据】的描述，为每一道题找到并提取对应的学生答案。
    **请注意**：纯文本学生作答可能只包含了题号（\"number\"）而没有包括题干，你需要根据【题目数据】中的题号（"number"）进行分割。纯文本学生作答也可能**未**包含题号，你需要通过作答间距等信息（但需要考虑可能同一题的解答分在了多页中的情况，以及页面顺序混乱时，你需要尝试不同页面顺序组合以尽可能进准完整的识别每个题目）以及作答内容的逻辑是否匹配【题目数据】的题干来进行推理后给出准确的分割。
    **务必注意**：学生可能会跳过不会作答的题目，因此需要与【题目数据】中的\"q_id\"及\"number\"保持数量及内容完全一致，这种情况\"content\"为空字符串。
    - `stu_ans`: 学生的全部可识别的题目作答，整合为一个列表，每个元素是一个字典，包含key:一个包含该生所有题目答案的列表，列表每个元素是一个json字典，包含key:\"q_id\"（题目唯一标识，来自【题目数据】）、\"number\"（题目作答中显示的题号，来自【题目数据】）、\"type\"（题目类型分类，来自【题目数据】）、\"content\"（识别得到的解答过程）、\"flag\"（识别异常情况，见下面详述）。
**重要指令：请保证提取作答内容\"content\"字段的完整性，你被禁止自行删减内容，也不允许进行翻译，请“完整”保留题干信息，你的工作只是将他们划分为多个题目。**

3.  **识别可信情况**: “答案分割”过程中对每个题目如有任何处理置信度不高或者完全无法处理的情况，需要给出**列表**包含所有可能存在的问题。
    - `flag`: 如题目作答页面混乱、题目作答不全等等。如果没有任何问题，则为空列表；如果有一个或多个问题，每个以字符串形式存入列表中。

4.  **格式化输出**：将上述所有处理好的1~3的信息整合成一个字典，key为\"stu_id\"、\"stu_name"\、\"stu_ans\", value分别为字符串、字符串、列表，其中列表每个元素是一个json字典，包含 "q_id", "number", "type", "content", "flag" 这几个字段。
例如，对于一个学生5道题目的作答，你的输出应该是这样的结构：
{
    "stu_id":"PB20111639",
    "stu_name":"张三",
    "stu_ans":
    [
        {"q_id": "q1", "number": "1.1", "type": "概念题", "content":"ans1", "flag":[]},
        {"q_id": "q2", "number": "1.2", "type": "计算题", "content":"ans2", "flag":["页面混乱,作答提取可能错误",]},
        {"q_id": "q3", "number": "2", "type": "编程题", "content":"ans3", "flag":["缺页",]},
        {"q_id": "q4", "number": "3.1", "type": "证明题", "content":"ans4", "flag":["缺页","识别乱码"]},
        {"q_id": "q5", "number": "3.2", "type": "推理题", "content":"ans5", "flag::[]},
    ],
}

**[重要指令]：你的回答必须是一个单一、完整且格式正确的JSON对象，应该直接以 { 开始，并以 `}` 结束。不要包含任何解释性文字、评论、或者Markdown的代码块标记（如```json）。 禁止在JSON对象前后添加任何前沿、导语、解释、注释、代码或任何非JSON内容，也不要包含换行符、tab符等。**
**[务必注意]**：在生成JSON时，请确保字符串值中的所有反斜杠 \"\\" 都被正确转义为 \"\\\"。这对于包含LaTeX公式的字段尤其重要。
"""

"""
**[注意事项]**:
- 你必须严格根据提供的 【题目数据】（一个JSON对象）来构建答案列表。输出字典中 'stu_ans' 中每一题的 `q_id`, `number`, 和 `type` 字段必须与【题目数据】中的信息完全匹配。
- 请仔细阅读【学生作答内容】，智能地识别学生对每个题目的解答。
- 如果在【学生作答内容】中找不到某个题目的明确答案，你仍然需要在答案列表中包含该题目的条目，但其 `content` 字段应设为空字符串。
- 你的最终输出必须是一个遵循上述指定JSON字典格式的单一对象。
**[重要指令]：你的回答必须是一个单一、完整且格式正确的JSON对象，应该直接以 { 开始，并以 `}` 结束。不要包含任何解释性文字、评论、或者Markdown的代码块标记（如```json）。 禁止在JSON对象前后添加任何前沿、导语、解释、注释、代码或任何非JSON内容，也不要包含换行符、tab符等。**
**[务必注意]**：不要包含任何换行符，只能是标准json格式字符串。
"""

example = '''
{
    "students":
    [
        {
            "stu_id":"stu1",
            "stu_name":"张三",
            "stu_ans":
            [
                {"q_id": "q1", "number": "1.1", "type": "概念题", "content":"ans1", "flag":[]},
                {"q_id": "q2", "number": "1.2", "type": "计算题", "content":"ans2",},
                {"q_id": "q3", "number": "2", "type": "编程题", "content":"ans3",},
                {"q_id": "q4", "number": "3.1", "type": "证明题", "content":"ans4",},
                {"q_id": "q5", "number": "3.2", "type": "推理题", "content":"ans5",},
            ],
        },
        {
            "stu_id":"stu2",
            "stu_name":"李四",
            "stu_ans":
            [
                {"q_id": "q1", "number": "1.1", "type": "概念题", "content":"ans1",},
                {"q_id": "q2", "number": "1.2", "type": "计算题", "content":"ans2",},
                {"q_id": "q3", "number": "2", "type": "编程题", "content":"ans3",},
                {"q_id": "q4", "number": "3.1", "type": "证明题", "content":"ans4",},
                {"q_id": "q5", "number": "3.2", "type": "推理题", "content":"ans5",},
            ],
        },
    ]
}
'''

def analyze_submissions(
    files_data: List[Dict[str, str]],
    problems_data: Dict[str, Dict[str,str]],
    student_store: List[Dict[str, Any]], # 接收学生存储字典的引用
    llm: Any, # 传入一个LangChain LLM实例
) -> List[Dict[str, Any]]:
    """
    使用 LangChain 分析学生提交的文件，提取学生信息并分割答案。

    Args:
        files_data: 解压后读取的文件数据列表。
                    格式: [{"filename": "20240101_张三.txt", "content": "这是第一题的答案..."}, ...]
        problems_data: 包含所有题目信息的JSON对象（或Python字典）。
                        格式: {"q1": {"q_id": "q1", "number": "1.1", ...}, ...]}
        llm: 已经初始化的 LangChain 聊天模型实例 (e.g., ChatZhipuAI).

    Returns:
        一个包含所有学生分析结果的字典，格式符合用户要求。
    """
    if not files_data:
        raise HTTPException(status_code=400, detail="输入的学生作答不能为空#1。")

    # 将 Pydantic 模型与 LLM 绑定，使其能够输出我们想要的结构
    # structured_llm = llm.with_structured_output(StudentSubmission)

    prob_main_data = []
    for prob in problems_data.values():
        prob_main = {}
        prob_main["q_id"] = prob["q_id"]
        prob_main["number"] = prob["number"]
        prob_main["type"] = prob["type"]
        prob_main["stem"] = prob["stem"]

        prob_main_data.append(prob_main)

    # 为了方便LLM处理，将题目数据字典转换为JSON字符串
    problems_json_str = json.dumps(prob_main_data, ensure_ascii=False, indent=1)

    all_students_results = []

    print(f"开始处理 {len(files_data)}份学生提交...")
    
    # Define a helper function for processing a single file
    def process_single_file(file_info):
        filename = file_info.get("filename", "")
        content = file_info.get("content", "")

        if not filename or not content:
            raise HTTPException(status_code=400, detail="输入的学生作答不能为空#2。")

        print(f"正在分析文件: {filename}")

        # 为每个文件构建一个 HumanMessage
        human_message_content = f"""
            请根据以下信息处理这份学生提交：

            **【文件名】**:
            {filename}

            **【题目数据 (JSON)】**:
            {problems_json_str}

            **【学生作答内容】**:
            ---
            {content}
            ---"""
        messages = [
            SystemMessage(content=SYSTEM_PROMPT),
            HumanMessage(content=human_message_content)
        ]

        # response_obj = structured_llm.invoke(messages)
        raw_llm_output = llm.invoke(messages).content
        json_output = parse_llm_json_output(raw_llm_output, StudentSubmission)
        logger.info(f"提取到学生解答:{json_output.model_dump()}")
        return json_output.model_dump()

    # Use ThreadPoolExecutor to process files in parallel
    # Limit the number of workers to avoid overwhelming the system
    max_workers = min(len(files_data), 16)  # Process up to 16 files in parallel
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        # Submit all file processing tasks
        future_to_file = {
            executor.submit(process_single_file, file_info): file_info 
            for file_info in files_data
        }
        
        # Collect results as they complete
        for future in concurrent.futures.as_completed(future_to_file):
            try:
                result = future.result()
                all_students_results.append(result)
            except Exception as e:
                file_info = future_to_file[future]
                filename = file_info.get("filename", "Unknown")
                logger.error(f"Error processing file {filename}: {e}")
                # Continue processing other files even if one fails

    stu_dict = {stu['stu_id']: stu for stu in all_students_results}
    student_store.clear()
    student_store.update(stu_dict)

    return stu_dict

@router.post("/")
async def handle_answer_upload(
    file: UploadFile = File(...),
    # 像其他端点一样，注入所需要的依赖
    problem_store: Dict = Depends(get_problem_store),
    student_store: List = Depends(get_student_store),
    llm: Any = Depends(get_llm)
    ):
    """
    接收上传的作业文件（压缩包或单个txt），提取内容，并交由AI分析。
    """
    logger.info(f"接收到文件: {file.filename}, 类型: {file.content_type}")
    
    try:
        file_bytes = await file.read()
        
        # 1. 使用新函数提取所有文件的内容
        # 这个函数会处理压缩包和单个文件
        files_data = await asyncio.to_thread(extract_files_from_archive, file_bytes, file.filename)
        
        if not files_data:
            raise HTTPException(status_code=400, detail="未在上传文件中找到有效的文本文件。")

        logger.info(f"成功从 '{file.filename}' 中提取了 {len(files_data)} 个文件。")

        # 2. 调用上一问中的 AI 分析函数 (此处为示意，您需要传入真实的 llm 和题目数据)
        # mock_llm = ... 
        # mock_problems = ...
        # analysis_result = analyze_submissions(files_data, mock_problems, mock_llm)
        
        # recognized_ans = analyze_submissions(
        #     files_data=files_data,
        #     problems_data=problem_store,
        #     student_store=student_store,
        #     llm=llm,
        # )
        
        recognized_ans = await asyncio.to_thread(
            analyze_submissions,
            files_data=files_data,
            problems_data=problem_store,
            student_store=student_store,
            llm=llm,
        )
        logger.info(f"成功分割作答内容：{recognized_ans}")

        return recognized_ans

    except ValueError as e:
        # 处理缺少库的错误
        logger.error(f"处理失败: {e}")
        raise HTTPException(status_code=501, detail=str(e))
    except RuntimeError as e:
        # 处理缺少 unrar 工具的错误
        logger.error(f"处理失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        logger.exception(f"处理文件 '{file.filename}' 时发生未知错误。")
        raise HTTPException(status_code=500, detail=f"处理文件时发生内部错误: {e}")
