# SmarTAI 整合交付包

**本地代码及测试完成；远端推送、新PR、评论未完成，原因是GitHub integration两项写入均返回403。**

代码SHA：`3977108642ce23fbbd149326c8e8df6d9c8ba6cf`。分支：`fix/verified-integration-20260920`。用户Mac工作区：`/tmp/smartai-integration-20260920-sMxfeA`。

## 六份主文档

| 内容 | 人读版 | AI版 |
|---|---|---|
| 修改、diff、提交及验证结果 | [01 人读版](01_完成与修改总结_人读版.md) | [01 AI版](01_完成与修改总结_AI版.md) |
| 合并、审核、base和05–13交接 | [02 人读版](02_合并审核与后续交接_人读版.md) | [02 AI版](02_合并审核与后续交接_AI版.md) |
| 当前暂行设计与旧附件差异 | [03 人读版](03_当前设计与原文档差异_人读版.md) | [03 AI版](03_当前设计与原文档差异_AI版.md) |

AI版以英文技术记录保留函数、状态与操作合同；人读版为中文说明。另有`manifest.json`、待发布`PR_BODY.md`/`REVIEW_COMMENT.md`、`publish_candidate.py`及其mock测试。

## 由已授权账号发布

以下命令由有仓库写权限的人在自己的终端运行；不向聊天发送任何密钥。先确保git与gh通过正常授权可访问仓库。

```bash
python3 publish_candidate.py --repo '/tmp/smartai-integration-20260920-sMxfeA/repo' --check
python3 publish_candidate.py --repo '/tmp/smartai-integration-20260920-sMxfeA/repo' --publish
```

脚本默认只读，显式`--publish`才普通推送、创建/复用main-based PR、写评论并请求审阅；不force、不合并、不批准、不关闭旧PR。main前进或非文档代码变动会拒绝发布，必须先重测。结果写在工作区delivery/publication-result.json，保留部分成功状态，勿当作全成功。

完整历史Git bundle留在Mac工作区`delivery/`；必须已拥有基础main才能使用增量bundle。下载包中的新增修复patch（如附）不含所有上游PR，不能直接当成main上的完整补丁。

测试原始日志、临时数据库及工具留在工作区；此包不包含个人凭据、数据库、node_modules、浏览器、字体文件或原始学生材料。后续的`delivery-receipt.json`记录文档提交和bundle校验值。
