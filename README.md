# Codex Sandbox MVP

公开实验仓库，用两个 ARM64 镜像验证一次性 Codex 沙箱闭环：

- `codex-api-manager`：常驻 API/SSE 与 Docker 生命周期管理服务。
- `codex-runner`：每个任务创建一个，任务结束后销毁。

## 构建

推送 `main` 或 `v*` 标签后，GitHub Actions 在原生 ARM64 Runner 上构建并推送：

```text
ghcr.io/<owner>/codex-api-manager:latest
ghcr.io/<owner>/codex-runner:latest
```

Codex 源码通过 `vendor/codex` Git Submodule 固定到具体提交。

## 本地配置

```bash
cp .env.example .env
```

修改 `.env` 后启动：

```bash
docker compose up -d
curl http://127.0.0.1:18080/health/live
curl http://127.0.0.1:18080/health/ready
```

当前提交先完成镜像构建、运行环境和健康检查。任务 API、SSE 和容器生命周期实现将在后续提交中加入。

Runner 已在 ARM64 服务器上通过一次性容器完成源码执行验证：克隆临时 Git 仓库、调用 Codex 修改 `README.md`、导出 JSONL 事件和 diff，然后自动删除容器。Runner 使用 Docker 容器作为隔离边界，容器内不再启动嵌套的 bubblewrap 沙箱。

如使用 Anthropic Messages 模型接口，需要在 Codex 与模型之间部署兼容 OpenAI Responses API 的协议转换层，并将 Runner 的 `base_url` 指向转换层。仓库目前不包含该转换层；`runner/config.toml` 中的模型地址仍是占位配置。

## Runner 生命周期

```text
API Manager
-> Docker Engine创建Runner
-> Runner执行codex exec --json
-> API Manager转发事件
-> 导出结果
-> 删除Runner和工作Volume
```

## Codex

上游项目：<https://github.com/openai/codex>

