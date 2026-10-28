# 合成样本说明

全部内容为本次构造，不是授权教师金标准或生产学生资料。

| 样本 | 用途 |
| --- | --- |
| `long_shared_definition.txt` | 27,756字符；真实24,000/400切块，第二题回指前置公共定义 |
| `duplicate_number_problems.txt` + `duplicate_number_rubric.txt` | 两个不同题干同号7，附加标准归属不确定 |
| `probes/test_mapping.py` 内三页材料 | 页面3、1、2乱序到达；原文每段恰一次且最终顺序1、2、3 |
| `probes/test_recovery.py` 内三种材料 | 题干3、1、2；标准2、1、3；标答1、3、2，某来源中页失败 |
| 同一恢复脚本的 `[1,7,1]` | 不同章节重复题号，成功材料复用、失败项两次重试、人工接手 |
| `probes/test_process_recovery.py` 动态三页PDF | 每页原生文字 `Q1/2/3. x = -2`；固定识别返回相同文字，测试独立进程硬退出及恢复 |

前三个文本文件的 SHA256 和字节数见 `manifest.json`。其余样本由可读脚本在本次临时运行根目录生成，不能以固定 OCR 响应证明图像识别准确率。进程样本的原始PDF、state、模型调用页号日志和结果JSON保留在启动器打印的 `runtime/pytest/` 路径下，数据库及原始运行目录不要入库。
