"""Shared interpretation contract, not a hard-coded list of user utterances."""

MATCHING_POLICY = """
SINGLE-SUBMISSION SET SEARCH CONTRACT (authoritative):
You interpret the teacher's wording into semantic fields, operators and SQL. The server
executes generic comparisons; it does NOT infer what a number means or choose a person.
No user clarification/candidate-picking dialogue for entity search. Return ALL matches,
including shared names. Zero matches is a successful empty result, never an invitation
to substitute another person. Never fuzzy-correct, use edit distance, pick the nearest
name, choose top-1, or relax a condition to make an empty query nonempty.

Reference fields and operators:
- student_name and student_id are DIFFERENT fields. A person-name fragment searches
  student_name; an ID request searches student_id, even when an ID consists of digits.
- Preserve a name phrase as ONE literal, including all its words. For an unqualified
  single name fragment use contains. A multi-word name uses exact unless the teacher
  explicitly asks for containment. Case and Unicode width are normalized, not spelling.
  Examples: alex / chen / ale -> student_name contains that literal; ale chen -> exact
  'ale chen', NOT 'alex chen'; Alex Wu -> exact 'Alex Wu'. Multiple Chens all match.
  IDs are strings; preserve leading zeroes. A complete ID uses exact; an explicitly partial ID can use contains/prefix/suffix.
- 'PB20开头的学号' -> student_id prefix PB20; '学号以56结尾' -> student_id suffix 56.
  A masked ID uses operator=pattern, with SQL LIKE mask semantics: _ matches ONE
  character, % matches an unspecified-length sequence, backslash escapes literal
  %, _ and backslash. Formatting spaces around a placeholder are not ID characters.
  In '123 *** 456', '123 xxx 456', '123 ... 456', the three written slots mean
  EXACTLY three characters, so value='123___456', NOT '123%456'. This is the default
  for a run of explicit slots. If the wording explicitly says 任意位数/不限长度,
  then use %. Honor explicitly literal x/asterisk/dot requests instead of masking.
  Generalize the wording; do not rely on any particular prefix, suffix, name or number.
- Reference.text is the actual source phrase. field, operator and value describe the
  intended key-value predicate; value never invents a name, ID or missing character.
- Questions use q_id or number and exact matching by default: Q4/第4题 select Q4, not
  Q40. Plain numbers are NOT automatically questions or IDs: use the nouns, units,
  current surface, header descriptions and surrounding operators to determine meaning.
- 分/得分/总分 mean absolute score/total_score. 得分率 and 百分比 mean 0..100
  percentage. 置信度/平均置信度 are 0..1 (80% means .8). Never interchange these.
- The original complete request wins over short-name guesses. Do not split 'Alex Wu'
  into 'Alex' and 'Wu', drop surnames, expand 'ale', or hide secondary constraints.
- Preserve AND, OR, NOT, inequality boundaries, ranges and top-N. When a named person
  is a comparison baseline, role=comparison. In 'Alex OR score below 60', or queries
  against class averages, set include_class_context=true: keep the whole population
  and perform the full Boolean condition in SQL. Resolve all referenced sets, not
  just the first one. Multiple matches and no matches never require clarification.
- A genuine unsupported operation (write/delete or nonexistent metric) returns a clear
  unsupported message, not guessed data. Do not ask the user to select a candidate.
"""

SURFACE_COLUMNS = {
    "student_analysis": {"学生/Student": "students.student_name + student_id", "总分/Total": "total_score (points)", "得分率/Rate": "score_percent (0..100)", "状态/Status": "score_percent >=60 passed, <60 failed, NULL unscored", "置信/Confidence": "avg_confidence (0..1)", "复核/Review": "review_count and pending_review_count", "Qn": "grades row identified by q_id: score, max_score, score_percent"},
    "question_analysis": {"题目/Question": "questions.number, q_id, type, stem", "作答/Responses": "response_count", "平均分/Mean score": "avg_score (points), max_score", "得分率/Rate": "avg_percent (0..100)", "置信度/Confidence": "avg_confidence (0..1)", "复核/Review": "review_count", "易错/风险": "grades.is_incorrect, review_reasons, comment"},
    "question_preparation": {"题号": "questions.q_id/number", "题型": "questions.type", "满分": "questions.max_score", "题目": "questions.stem", "标答": "questions.reference_answer", "评分标准": "questions.criterion", "测试样例": "test_cases", "审核提示": "preparation_issues (status=open)"},
    "submission_review": {"学号": "students.student_id", "姓名": "students.student_name", "Qn作答状态": "answers.state for that q_id", "身份": "students.identity_status", "覆盖率": "recognized nonempty answers divided by questions"},
    "student_answer_review": {"题号": "questions.number/q_id", "题型": "questions.type", "题干": "questions.stem", "答案": "answers.content", "状态": "answers.state/review_status; current student context is fixed"},
    "review_overview": {"姓名/学号": "students.student_name/student_id", "Qn复核状态": "grades.reviewed, requires_review, score, confidence for that q_id", "批注": "grades.teacher_comment", "总分": "students.total_score"},
    "history": {"任务": "tasks.name", "当前阶段": "tasks.status", "最近更新": "tasks.updated_at", "课程": "tasks.course_name/course_id", "学期": "tasks.semester_id", "标签": "task_tags.tag_name", "进度/预计剩余": "live-only UI fields; not present in the persisted snapshot, never invent"},
    "chart": {"可用数据": "all task business schema; bind chart axes/series to query columns, not generated arrays"},
}
