# 已有 PR 整合与实质／安全修复

## 候选

- base: `main@edfdabceca369ccba3f1d62178908303b8f8dec4`
- head branch: `fix/verified-integration-20260920`
- 测试对应的代码提交: `3977108642ce23fbbd149326c8e8df6d9c8ba6cf`。后续只增加交付文档/脚本；实际PR head以GitHub显示为准。
- 包含 #61–#67 的有效代码及 #60，#59 的原提交通过 #61 保留；#60 的无关文档删除排除。#40旧mock壳和#58线程PDF退步不合入。
- 这是一个 main-based 整合PR，不再要求逐个改变旧堆叠PR的base。不合并/批准旧PR来绕过本轮修复。

## 项目负责人已批准的暂行设计

一道大题=一个q_id/计分/生成/恢复单元，小问为内部metadata；并发复用BYOK max_concurrent、RPM与endpoint上限。完成可清理原件，但识别重试或结构化输入不全时保留；新文件成功保存并原子替换后旧件可清。详见docs/integration/20260920下03号两版文档。

## 修复

数学括号误拆、扣分累加、编辑后旧结构、首个小问资料标签、reporter所有权、密码重置响应后投递、PDF取消回收、失败来源/压缩包保留、production宿主代码执行拒绝、敏感traceback及前后端依赖安全版本。新增补丁在7个聚焦提交中；27文件是本轮新增修复，整合的220文件主要是已有PR成果。

## 验证

新依赖环境完整后端1460 passed/30 skipped；干净npm ci后80文件378前端测试、typecheck/scope/build通过；真实本地PostgreSQL16.15的23测试及迁移往返通过；本地浏览器E2E2通过；SDK/网络43通过；当前前后端依赖审计零告警。各范围有重叠，不相加。Mac上3个Linux特有worker原语测试跳过，同字节在Linux中3项通过，但不等于OCI验收。

## 审核与合并

建议lyj或合格非作者重点审认证/session/迁移；文件恢复、配额和删除安全由一名合格审阅者检查，可为同一人。ljd/dsy在后续接线前读major/source/artifact合同，不必等05–13完成。满足当前仓库正式review规则后，人手工合并本整合PR；确认main包含候选后，再处理仍open的旧PR。脚本不批准、不合并、不关闭任何PR。

## 边界

真实Gmail、真实模型/OCR、AWS/S3、Windows、完整OCI和学校发布验收仍unverified。production旧宿主执行现已拒绝，不能宣称正式08/09已完成。密码找回的响应后任务不是持久邮件队列，进程退出可能需用户重新申请。依赖零告警不是绝对无漏洞保证。

## 发布记录

交付时GitHub branch创建和#61评论均被integration HTTP403拒绝；本正文为待发布材料，不能把交付时状态写成已推送。使用发布脚本成功后，以publication-result.json和实际PR URL/HEAD为准。
