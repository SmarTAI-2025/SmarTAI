# 后端待实现接口契约（前端调用约定）

> **2026-04-26 更新**：第 1-5 节、第 10 节的端点**已全部实现**（在 [backend/api/auth.py、users.py、courses.py、assignments.py、students.py](../backend/api/) 中）。详见 [backend/BACKEND_P0_NOTES.md](../backend/BACKEND_P0_NOTES.md)。
> 本文档仍然是**前端的调用约定**，未实现的部分（P1 文件存储、报告导出、通知、RAG）保留为待办。

> 本文档列出 `frontend_v2/` Reflex 前端调用、但当前 `backend/` 未实现的端点。
> 后端实现时**严格遵守这里列出的请求/响应 schema**，前端代码已按此假设编码。

---

## 1. 认证（P0，最优先实现） — ✅ 已实现

### POST /auth/register
请求体：
```json
{ "username": "teacher01", "password": "...", "email": "...", "role": "teacher", "invite_code": "OPTIONAL" }
```
响应：
```json
{ "user_id": "u_xxx", "token": "<JWT>", "user": { "id": "u_xxx", "username": "teacher01", "role": "teacher", "email": "..." } }
```

### POST /auth/login
请求体：
```json
{ "username": "...", "password": "..." }
```
响应：
```json
{ "token": "<JWT>", "user": { "id": "...", "username": "...", "role": "teacher|student|admin", "email": "..." } }
```
失败：`401 { "detail": "Invalid credentials" }`

### POST /auth/refresh
头：`Authorization: Bearer <token>`
响应：`{ "token": "<new JWT>" }`

### POST /auth/logout
头：`Authorization: Bearer <token>`
响应：`{ "status": "success" }`

### GET /auth/me
头：`Authorization: Bearer <token>`
响应：`{ "id": "...", "username": "...", "role": "...", "email": "...", "created_at": "..." }`

---

## 2. 用户管理（P0）

### GET /users/
角色：admin、teacher
响应：`[{ "id": "...", "username": "...", "role": "...", "email": "..." }, ...]`

### POST /users/invite
角色：teacher、admin
请求体：`{ "email": "...", "role": "student|teacher", "course_id": "OPTIONAL" }`
响应：`{ "invite_code": "...", "expires_at": "..." }`

### PATCH /users/{user_id}
请求体：`{ "username": "...", "email": "...", "role": "..." }`（部分字段）

### DELETE /users/{user_id}

---

## 3. 课程/班级（P0）

### GET /courses/
响应：
```json
[{ "id": "c_xxx", "name": "高等数学A", "code": "MATH101", "teacher_id": "...", "student_count": 42, "created_at": "..." }]
```

### POST /courses/
请求体：`{ "name": "...", "code": "...", "description": "..." }`

### GET /courses/{id}
### GET /courses/{id}/students → `[{ User }]`
### POST /courses/{id}/enroll
请求体：`{ "student_ids": ["..."], "invite_code": "OPTIONAL" }`

---

## 4. 作业（P0）

### POST /assignments/
请求体：
```json
{ "course_id": "...", "name": "第三章作业", "description": "...", "due_at": "...", "problem_data": { "<q_id>": { ... } } }
```
（`problem_data` 复用现有 `/prob_preview/` 返回的 dict 格式）
响应：`{ "id": "a_xxx", "status": "draft" }`

### GET /assignments/?course_id=...&status=published|draft|all
响应：作业列表

### GET /assignments/{id}
响应：完整作业（题目 + 元信息）

### POST /assignments/{id}/publish
响应：`{ "status": "published", "published_at": "..." }`

### POST /assignments/{id}/submit
角色：student
请求：multipart/form-data，`file: <学生答案文件或压缩包>`
响应：`{ "submission_id": "s_xxx", "status": "submitted" }`

### GET /assignments/{id}/my_submission
角色：student
响应：`{ "submission_id": "...", "submitted_at": "...", "files": [...], "grade": null }`

---

## 5. 学生查分（P0）

### GET /students/me/grades
角色：student
响应：`[{ "assignment_id": "...", "assignment_name": "...", "score": 85, "max_score": 100, "graded_at": "..." }]`

### GET /assignments/{id}/my_grade
角色：student
响应：完整批改详情（与教师视角一致，但隐藏其他学生）

---

## 6. 文件存储（P1）

### POST /files/upload
请求：multipart/form-data
响应：`{ "file_id": "f_xxx", "url": "https://oss.../...", "size": ..., "mime": "..." }`

### GET /files/{id}
响应：文件流（带 Content-Disposition）

---

## 7. 导出报告（P1）

### GET /reports/job/{job_id}.pdf
响应：PDF stream

### GET /reports/job/{job_id}.xlsx
响应：Excel stream

### POST /reports/student/{student_id}/transcript
请求体：`{ "course_id": "...", "term": "..." }`
响应：PDF stream

---

## 8. 通知（P1，可暂用 GET 轮询）

### GET /notifications/?unread_only=true
响应：`[{ "id": "...", "type": "grading_done|assignment_published|...", "title": "...", "body": "...", "link": "...", "read": false, "created_at": "..." }]`

### POST /notifications/{id}/mark_read
### POST /notifications/mark_all_read

---

## 9. RAG 知识库（P2，前端先用 stub）

### POST /rag/kb
### GET /rag/kb/
### GET /rag/kb/{id}/files
### POST /rag/kb/{id}/upload
### POST /rag/search

---

## 10. 鉴权依赖

后端补 [backend/api/auth.py](../backend/api/auth.py) 时，导出一个 `get_current_user(token: str = Depends(...))` 依赖，所有受保护端点（即除 `/auth/login`、`/auth/register`、`/`、`/health` 外）都注入这个依赖。

现有 `/ai_grading/*`、`/prob_preview/`、`/hw_preview/`、`/experts/*`、`/human_edit/*` 端点路径**保持不变**，仅在内部追加 `current_user: User = Depends(get_current_user)`。

CORS（[backend/main.py:73-80](../backend/main.py#L73-L80)）需要把 `http://localhost:3000` 加入 `frontend_urls`。
