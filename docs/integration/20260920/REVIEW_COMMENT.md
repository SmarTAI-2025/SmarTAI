## 审核范围、合并顺序与后续接口

项目负责人已明确接受major-question-only、BYOK并发和按恢复需要保留原件的当前方案。不要要求为了对齐旧附件而重新实现leaf或专用1～2并发。当前测试代码提交`3977108642ce23fbbd149326c8e8df6d9c8ba6cf`，实际PR head应仅追加文档。

**建议审核**：lyj或同等能力的合格非作者审认证/会话失效/迁移及SDK依赖升级；文件清理与配额由熟悉事务的审阅者重点看，也可同一人兼任。检查新文件成功采用前不删除旧件、识别失败与必要容器保留、结构化输入完整性、旧worker不可覆盖/误删。没有要求所有05–13成员完成任务或普遍通读全部代码。

**后续接线前必须阅读**：ljd读03方案和source/artifact/retained合同，dsy读major q_id/小问片段及评分未执行语义；xts保留production无宿主回退，08/09仍需正式OCI；13复用现有auth/session失效。完整表格见`docs/integration/20260920/02_合并审核与后续交接_人读版.md`及AI版。

**唯一推荐合并顺序**：当前整合PR（base=main）通过required review后人工合入→核对main包含candidate→解释性关闭仍open的旧#40/#58/#59/#60–#67。不要再把旧栈原样先合入；不需要每层改base。main若前进，先整合并重测受影响范围，不能复用过期证据。

**证据**：后端新环境1460 passed/30 skipped，前端378，PostgreSQL23及迁移往返，E2E2，当前依赖审计零告警。没有以AI检查冒充submitted human approval；未做真实Gmail/模型/AWS/S3/完整OCI/Windows/学校验收。

本评论不是批准或合并；绝不保证所有输入/未来环境无错误。关键代码、安全和合并规则仍须按当前head实际检查。
