# 编程题 U2 本地验收基线

先读 [简洁基线报告](BASELINE.md)、[可复用验收清单](CHECKLIST.md) 和 [验证记录](VALIDATION.md)。工具是观察器：复现已有行为、记录输入及输出，不实现同学负责的07/08/09和新增三项。

U1：[准备编程题验收样本包](codex://threads/01a180ed-d804-7cf2-89ab-920ac32fe354)；U3：[核查旧材料关联与恢复问题](codex://threads/01a180f0-3f9a-76d1-92ab-1f3b20ce1c67)；U2：[建立编程题 U2 验收基线](codex://threads/01a180f2-93ae-7662-9a91-3bd9d1d4ec9d)。U1/U3版本及PR见验证记录。不要复制未确认分数为教师金标准。

## 复跑

使用干净、无 `.env` 的 Mac 本地 checkout，安装与该 SHA 锁一致的依赖。本次 Python 是 `/opt/anaconda3/envs/smartai-py312/bin/python`，实测 Node `26.10.0`（仓库 `.node-version` 为 `22.16.0`，未安装新版本；不声称已验证 Node 22）。命令中的 `U1_PACKET` 指向已记录版本的 U1 样本目录；不要指向生产数据。输出目录用新的临时目录。

```sh
U2_REPO="$PWD"
U2_PYTHON=/opt/anaconda3/envs/smartai-py312/bin/python
U1_PACKET=/Users/annie/code/SmarTAI/manual_test_samples/programming_u1_20261028
"$U2_PYTHON" tools/evaluation/programming_u2/probe_baseline.py --repo "$U2_REPO" --u1 "$U1_PACKET" --out /private/tmp/u2-repeat-programming.json
"$U2_PYTHON" tools/evaluation/programming_u2/workflow_probe.py --source "$U2_REPO" --output /private/tmp/u2-repeat-workflow
```

第一个脚本调用真实技能/比较逻辑，但模型、runner边界均固定回复，禁止网络及子进程；第二个使用真实挂载HTTP接口和临时SQLite，只有已明确标记的批改batch是固定回复。它只允许自身读回子进程与读取Git SHA，不执行学生代码或runner。某些检查断言的是**当前缺陷依然存在**，修复后须查看原始观察并更新基线，不能以脚本退出码代替产品验收。

既有无子进程后端子集：精确命令与排除项在验证记录。`run_offline.py` 禁止外网和子进程，任何拒绝均使整次结果失败；不要使用它把真实OCI或宿主执行用例当成通过。

浏览器：先在独立终端由当前 checkout 启动测试前端。确认5188没有其他任务服务；不要复用不明来源端口。

```sh
cd frontend/app
VITE_SMARTAI_BACKEND_URL=http://127.0.0.1:8188 npm run build -- --mode e2e
npm run preview -- --host 127.0.0.1 --port 5188 --strictPort
# 另一个终端在仓库根目录：
node tools/evaluation/programming_u2/browser_baseline.mjs "$PWD/frontend/app" /private/tmp/u2-repeat-browser http://127.0.0.1:5188
```

浏览器脚本只允许本机前端资源，8188全部由固定API夹具拦截，不需要后端监听该端口。它记录请求、DOM、截图、控制台及未知请求；这是原界面的可重复交互验证，不能证明真实持久化、模型判分或OCI执行。本次以同SHA的预构建前端进行观察，启动与文件hash证据见验证记录。

## 文件与边界

- `probe_baseline.py`：U1合成样本、固定回复，记录20项技能及执行合同观察与消费文件hash。
- `workflow_probe.py`：真实HTTP/SQLite保存、409、进程重开、上传排队、批改输入与固定结果。
- `browser_baseline.mjs`：原控件保存/刷新、函数用例反例、缺失用例和4类结果显示。
- `run_offline.py`：现有回归的无凭据/无外网/无子进程运行器。
- `evidence/`：本轮静态观察快照；截图只保留有助复现函数编辑问题的证据。完整临时数据库不提交。

不修改产品逻辑，不操作生产数据，不运行真实模型或OCI；任何真实调用、资源环境变更及生产验收均需另行明确范围。PR自动CI/Pages预览经用户允许，开发和主动测试仍在Mac本地，PR不合并。
