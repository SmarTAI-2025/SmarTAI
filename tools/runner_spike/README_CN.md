# XTS-W1-RUNNER-SPIKE

本原型验证一个独立 OCI 容器能否承接现有 `RunnerRequest` / `RunnerResult`
接口。它不接网页，也不替换当前生产执行路径。

## 运行

在仓库根目录执行：

```bash
python tools/runner_spike/harness.py doctor --runtime docker
python tools/runner_spike/harness.py build --runtime docker
python tools/runner_spike/harness.py probe --runtime docker
```

Oracle 若使用 Podman，把三条命令中的 `docker` 改为 `podman`。只有四项检查
全部显示 `passed: true` 才算候选运行时通过：

- `production_env_cleared`：宿主进程持有哨兵密钥，但学生代码看不到任何生产前缀环境变量；
- `network_disabled`：外部 TCP 和 DNS 都失败；
- `directory_isolated`：宿主哨兵不可见，根目录和输入只读，只有 `/work` 可写；
- `process_limit`：超过 `--pids-limit=16` 后创建进程失败。

每次运行还固定使用只读根文件系统、只读单文件输入挂载、临时 `/work`、非 root
用户、丢弃全部 capabilities、`no-new-privileges`、CPU/内存/文件/输出/墙钟限制，
并在结束或超时后按唯一容器名强制清理。容器镜像只包含 Python、SymPy 和独立
worker，不包含 FastAPI 代码或网站配置。

## 当前验证结论（2026-08-11）

当前开发机没有 Docker/Podman；`unshare --net` 和 `unshare --pid` 返回
`Operation not permitted`，cgroup 根也不可写。因此宿主 `unshare` 方案已否决，
本机不能伪称完成隔离验收。仓库的 `Runner isolation spike` CI 会在真实 Docker
环境构建镜像并执行全部四项探针；Oracle 上仍须由 gsy 用实际运行时复跑同一命令。

风险结论：OCI 容器是可继续进入 Week 2 的候选，但本原型不能直接上线。正式接入
前还需在 Oracle 固定镜像 digest、确认 rootless Podman/Docker 与默认 seccomp、
设置全局容器并发上限、完成一次性内部调用凭证和 `/ready` 能力状态，并验证宿主
重启/runner 崩溃后的回收。上述任一项未通过时，公网能力必须保持 `disabled` 或
`degraded`，不得回退到宿主机直接执行不可信代码。
