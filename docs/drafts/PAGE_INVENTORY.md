# 显式本地暂存：页面覆盖清单

核对范围：`src/main.tsx` 全部生产路由、路由的编辑组件、弹窗与浮层；另核对 `routes/teacher`、`routes/student`、`routes/admin` 保留代码，以及开放 PR #117、#124–128、#130、#131。本表区分实际启用页面与尚未合并的独立入口，不能据此宣称所有未来页面已经上线。

| 路由／编辑组件 | 未提交业务输入 | 分类与接入 |
|---|---|---|
| `/tasks/new`、`/tasks/:id/edit` | 名称、学期、课程选择与待创建名称、标签选择与待创建名称、创建幂等标识 | 需要暂存；统一显式按钮与离开保护，课程/标签创建成功仍是正式保存 |
| `/tasks/:id/upload/problems` | 全部来源、角色、题目/评分标准/参考答案/编程答案/测试资料文件、页码/目标范围、分值策略、识别模型、有效预检引用 | 需要暂存；实际文件字节或服务器引用；引用 GET 校验；不自动预检/识别 |
| `/tasks/:id/questions/:qid/:section` | 题干、满分、评分标准、参考答案、编程答案、可见/隐藏测试用例 | 需要暂存；每个独立正式保存字段单独生命周期；取消编辑同一三选项；暂存不确认题目复核 |
| `/tasks/:id/questions/import` | 教材/资料选择、真实上传文件、提取目标/方式/提示、OCR 和入库选项 | 需要暂存；恢复不启动提取；正式开始成功清除 |
| `/tasks/:id/questions/import/review/:job` | 接受的提取项目与覆盖授权 | 需要暂存；绑定任务与作业计划版本；暂存不应用计划 |
| `/tasks/:id/questions/ai-complete` | 目标题目及测试用例数量 | 需要暂存；暂存不生成答案或测试 |
| `/tasks/:id/submissions/upload` | 学生作答真实文件、学生名单真实文件、身份匹配方式、识别模型 | 需要暂存；任务与课程隔离；正式开始成功清除 |
| `/tasks/:id/students/:student` | 身份 ID/姓名、每题作答编辑 | 需要暂存；学生与每题隔离；身份信息属个人数据，仅明确暂存、限定保留期/登出清理；暂存不确认身份/作答 |
| `/tasks/:id/grading-setup` | 专家/样本数、策略、教师指令、其他批改设置 | 需要暂存；恢复校验与步骤状态；暂存不开始批改 |
| `/tasks/:id/review/:student/:qid` | 教师分数、评语 | 需要暂存；每题生命周期；正式保存保留确认与 expected workflow revision 契约；暂存不确认复核 |
| `/tasks/:id/results/visualizations` | 查询文本、已生成预览、选定图表 | 需要暂存；恢复已生成 JSON，不自动重复收费查询 |
| `/knowledge-base` 上传弹窗 | 真实文件、课程/分组/分类/标签、原生解析选项 | 需要暂存；关闭按钮/Escape/遮罩统一保护；上传成功清除 |
| `/knowledge-base` 新建/编辑分组弹窗 | 名称与课程归属 | 需要暂存；分组 ID 与服务器元数据版本隔离 |
| `/knowledge-base` 编辑资料弹窗 | 名称/课程/分组/分类/标签 | 需要暂存；版本冲突提示；保存/删除成功清除；删除确认勾选不作为草稿 |
| `/history` 标签浮层 | 待创建/编辑名称与颜色 | 需要暂存；任务隔离、浮层关闭保护；选用/取消已有标签即时正式保存，不增加另一个业务草稿 |
| `/settings/byok` 模型编辑、百度 OCR 凭据 | API key/secret、模型配置中的同表单字段 | 敏感特殊；只做未保存离开/关闭保护；不写普通草稿、IndexedDB、sessionStorage 或 localStorage |
| `/login`、`/register`、`/forgot-password`、`/reset-password` | 密码/邮箱及认证输入 | 敏感特殊；只在当前表单内存保留并提示离开；成功沿用原认证流程清理，不持久化认证秘密 |
| `/settings/account` | 当前只读账号与本机草稿管理 | 展示不适用；提供发现/打开/主动删除草稿入口 |
| `/`、`/history` 搜索/筛选/排序/Ask；知识库搜索/筛选 | 查询条件，非尚待正式保存的业务结果 | 不适用独立草稿；按已有查询/URL状态；标签编辑见上行 |
| 题目/作答/结果复核总览的确认操作、历史任务删除、模型启停/排序/默认设置、知识资料重新识别确认 | 立即执行的明确业务动作 | 即时正式保存，不另造草稿；保留已有版本、幂等及保存中保护 |
| 所有 progress、preflight、overview、results 的题目/学生/报告/下载页、PDF/原件预览、错误/找不到页面、学生不可用页 | 展示/导航/下载/阅读位置/立即开始按钮 | 展示不适用；预检倒计时/确认开关不是需要跨访问保留的业务输入 |
| `/register/check-email`、`/register/verify`、找回密码查收页及所有 redirect 别名 | 验证状态与只读结果 | 不适用；链接 token 不暂存 |

## 未启用代码与平行 PR

生产 `main.tsx` 未导入或提供 `/teacher`、`/student`、`/admin` 业务路由，范围审计明确将这些源文件保留为 dormant。不能在本任务悄悄启用入口。

| 保留源文件/开放 PR 页面 | 清单判断 | 当前启用状态 |
|---|---|---|
| TeacherCoursesPage 课程新建、TeacherCourseDetailPage 名单与作业新建 | 将来启用时需要暂存 | 当前无路由；本次不开放 LMS/课程管理 |
| TeacherAssignmentDetailPage / AssignmentEditorPage 题目、批量 JSON、题目文件 | 将来启用时需要暂存 | 当前无路由；不与正在使用的任务题目流程混淆 |
| TeacherSubmissionsPage 手工/文件作答、StudentAssignmentPage 学生作答/文件 | 将来启用时需要暂存，学生信息特殊处理 | 当前无路由；学生端仍不可用 |
| TeacherGradingPage 分数/评语 | 将来启用时需要暂存 | 当前无路由；AssignmentGradingPage 只有立即开始/发布动作 |
| 教师/学生其余 Dashboard、结果、课程展示 | 展示不适用 | 当前无路由 |
| 遗留 AdminInvitesPage 角色/邮箱 | 将来启用时需要暂存；生成的邀请码是认证秘密，不暂存 | 当前无路由；AdminUsersPage 筛选即时查询，AdminSystemPage 只读 |
| PR #117 私有 AdminBusinessConfigPage 全局规则/用户配额/原因 | 需要暂存；应复用统一能力并替换旧两选项 blocker | 独立私有入口尚未合并；按原 expected_version / expected_global_version / 幂等 / 审计原因契约接入，组合验证与交叉文件另列 |
| PR #117 用户生命周期/封禁/销户/维护操作、管理员改密码 | 操作确认不应持久化；密码特殊，只做离开保护 | 不改授权/会话/数据库/维护功能；不能把销户确认暂存成自动授权 |

新增页面必须按这份分类选择共享适配器，不能仅因为有 input 就长期保存。 dormant 页面启用和开放 PR 合并后，需要相应路由及组合验收；本 PR 不声称已测试未来入口。
