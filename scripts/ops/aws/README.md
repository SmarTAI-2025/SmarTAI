# AWS 后端与编程沙箱一起发布

GitHub Actions 继续使用现有的 `Deploy AWS backend`、部署 SSH 密钥和受限 SSH 命令。通过 CI 的 main 提交由 Lightsail 下载和部署。`smartai-deploy` 是服务器发布命令的维护源。镜像在 Lightsail 以现有 `smartai` 账号和 rootless Podman 构建，因此 GitHub 和 AWS 都无需配置镜像仓库。

每次发布先在目标版本验证镜像。在构建输入相同的情况下复用本机镜像。验证包括四项隔离检查、Python 函数返回、标准输入和请求校验。验证通过后，把不可变镜像 ID 写入该版本的 `.runner.env`，再切换 `/opt/smartai/current`。systemd 从这一目录读取镜像 ID。启动后同时检查两个服务的 `/ready` 和线上进程实际加载的镜像 ID。

未执行数据库迁移时，启动或就绪失败会把后端和镜像一起切回上一次发布，并检查恢复结果。已经执行数据库迁移时，保留原有的备份和人工恢复流程。已发布版本的镜像应与对应版本一同保留，供回滚使用。

## 首次启用

1. 在 GitHub 审核这个目录下的发布工具和测试。保留现有 GitHub Actions Secrets 与 SSH 配置。
2. 合并前，把审核过的 `smartai-deploy`、`40-release-runner.conf` 和 `install-deploy-tool` 复制到 Lightsail 上的管理员暂存目录。用管理员账号运行以下一次性安装。校验值对应本次改造前审核过的服务器脚本。如果不符，先比对服务器版本再安装。

   ```bash
   sudo bash ./install-deploy-tool f2cd09ac86cad6a4097053de9f118d01577b9656f02824f99936a31f3740dd7d
   ```

   安装会备份原发布脚本，为 `smartai.service` 增加一条读取配置的规则，并核对现有服务。它保留 `30-oci-runner.conf` 中的镜像，作为第一次配套发布之前以及回滚到旧目录时的后备。安装只重读 systemd 配置，不重启应用。
3. 合并 PR，等待 main 的 CI 与原部署流程。核对部署日志中的候选检查、两个 `/ready` 和进程镜像，以及 `current` 对应目录的 `.runner.env`。首次上线后，无需手改镜像 ID、追加 GitHub Secrets 或单独重启。

之后修改发布工具本身时，审核后同样由管理员安装该工具。项目的正常后端与沙箱镜像更新由已安装的工具自动处理。
