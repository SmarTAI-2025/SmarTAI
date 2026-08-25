# 百度 Unlimited-OCR BYOK：ljd 最小验收说明

> 候选分支：`codex/baidu-unlimited-ocr-byok-20260823`
>
> 验收对象：本文提交所在的精确 SHA
>
> 合并条件：ljd 回复“通过”，或指出一个可复现的具体问题；在此之前不合并。

## 1. 本次要验什么

本 PR 是 **BYOK-only 后端 provider 骨架**。请 ljd 只验以下四项：

1. **代码与契约**：owner 凭据隔离、AK/SK 加密保存、日志/错误脱敏、固定百度官方 endpoint、提交不自动重放、结果下载 URL fail-closed。
2. **无隐式启用**：没有平台共享密钥、公共模型池、默认 OCR/vision selector、前端入口或正式任务路由。
3. **真实接口最小 smoke**：用一份合成或去标识的小文件成功完成一次提交、轮询和 Markdown 下载；`/verify` 只换 access token，不能替代这一步。
4. **OCR 领域判断**：样本的页序、正文和公式是否达到“可供教师继续校对”的水平，并记录一个简短结论。

这次不验正常产品 UI、Agent/task 路由、真实学生数据、并发压测、共享账号或生产上线。它们尚未接入，也不是本骨架 PR 的完成声明。

## 2. 凭据和样本安全

- AK 是百度 OCR 应用的 API Key；SK 是同一应用的 Secret Key。
- 谁拥有 AK/SK，谁就在自己的电脑和 owner 会话里运行。不要把 AK/SK、access token、Cookie 或登录会话发给别人，也不要贴进聊天、截图、命令历史或仓库。
- ljd 可以使用自己的百度账号和应用凭据；如果凭据属于 Annie，则由 Annie 本地运行 smoke，只把脱敏后的结果交 ljd 判断。
- 样本优先使用人为制作的假作业；或使用已删除姓名、学号、学校、班级、二维码、条形码等身份信息的文件。建议一页、通用文件名、内容不敏感。
- 百度官方说明成功和失败调用都可能消耗免费测试页数，所以只提交一次，不反复试跑。

在百度文字识别控制台只需确认并记录这些非秘密事实：

- 应用是否已勾选“文档解析（Unlimited-OCR）”权限；
- 资源列表中该能力是否可用；
- 剩余页数和有效期（页面有显示才记录，没有就写“未显示”）。

不要发送带账号名、应用 ID、AK/SK 或其他个人信息的完整控制台截图。可以裁剪/遮挡后只保留能力名称、状态、剩余页数和有效期。

## 3. ljd 操作步骤

1. checkout 本文顶部候选分支，并在回复中写下实际测试的完整 SHA。
2. 在仓库根目录运行聚焦测试：

   ```bash
   /opt/anaconda3/envs/smartai/bin/python -m pytest \
     backend/tests/test_baidu_unlimited_ocr_tool.py \
     backend/tests/test_baidu_unlimited_ocr_credentials.py -q
   ```

3. 完成上面的控制台非秘密状态确认。
4. 用自己的凭据运行一次 direct smoke。当前没有文件识别产品 API/UI，因此不要从正常产品页面测试。可把下面代码临时保存在仓库外的 `/tmp/baidu_ocr_smoke.py`；`getpass` 不会把 AK/SK 回显或写入命令历史：

   ```python
   import asyncio
   from getpass import getpass
   from pathlib import Path

   from backend.skills.ocr_ingest import BaiduUnlimitedOCRSkill
   from backend.tools.baidu_unlimited_ocr import (
       BaiduUnlimitedOCRClient,
       BaiduUnlimitedOCRError,
   )


   async def main() -> None:
       sample = Path(input("合成/去标识样本路径: ").strip()).expanduser()
       try:
           async with BaiduUnlimitedOCRClient(
               api_key=getpass("Baidu API Key: "),
               secret_key=getpass("Baidu Secret Key: "),
           ) as client:
               result = await BaiduUnlimitedOCRSkill(client).recognize_document(
                   sample.read_bytes(), sample.name
               )
           print(f"provider={result.provider} model={result.model}")
           print(result.text[:1000])
       except BaiduUnlimitedOCRError as exc:
           print(f"safe_error={exc.code} retryable={exc.retryable}")


   asyncio.run(main())
   ```

   从仓库根目录执行：

   ```bash
   PYTHONPATH=. /opt/anaconda3/envs/smartai/bin/python /tmp/baidu_ocr_smoke.py
   ```

5. 只保留脱敏证据：精确 SHA、样本类型/页数、成功或安全错误码、耗时大致范围、最多一小段不含身份信息的 OCR 输出。若结果 URL 被拒绝，只记录 hostname，不要复制带签名参数的完整 URL。

## 4. 通过标准

- 两个聚焦测试文件全部通过。
- 真实调用得到非空 Markdown；返回 `provider=baidu_unlimited_ocr`、`model=None`。
- 输出对该合成/去标识样本基本可读，页序正确，正文与公式足以让教师继续校对；明显不确定内容可以指出，不要求模型零错误。
- 日志、终端和回复中没有 AK/SK、token、签名 URL 或原始厂商敏感错误。
- 没有观察到共享密钥、默认 selector 或 silent fallback。

如果真实调用失败，不要换样本反复提交。请回复安全错误码、测试 SHA、文件格式/页数和复现阶段；由开发者先定位。

## 5. 回复模板

```text
结论：通过 / 不通过
测试 SHA：
聚焦测试：
控制台：Unlimited-OCR 权限=是/否；资源可用=是/否；剩余页数/有效期=（可见才填）
样本：合成或去标识；格式；页数
direct smoke：成功 / safe_error=<code>
OCR 质量一句话：
具体问题（没有则写“无”）：
```
