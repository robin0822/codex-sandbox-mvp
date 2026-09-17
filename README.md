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

API Manager 现已支持最小任务接口：`POST /v1/tasks` 接收 HTTPS 仓库、分支和 Prompt，创建一次性 Runner；`GET /v1/tasks/{task_id}` 查询状态；`GET /v1/tasks/{task_id}/events` 实时推送 Codex 事件；`GET /v1/tasks/{task_id}/result` 返回最终回答、退出码和 diff；`GET /v1/capacity` 查看单机并发。Runner 完成或超时后由 Manager 删除，结果文件暂存于 `/data/codex-mvp/results/{task_id}`。

仓库首次使用时由 Manager 从远程创建裸仓库缓存，保存在 `/data/codex-mvp/repo-cache`。之后相同 URL 的任务不访问远程仓库：Runner 只读挂载缓存，在自己的容器内创建独立副本并检出记录的 commit。分支后续更新不会自动进入缓存；需要最新代码时，在请求的 `repository` 中传入 `"refresh": true`，本次任务会先增量更新缓存。每次任务仍使用全新的 Runner 和工作目录。首次远程克隆最多等待 120 秒。

实验请求示例：

```bash
curl -sS http://127.0.0.1:18080/v1/tasks \
  -H 'Content-Type: application/json' \
  -d '{"repository":{"url":"https://github.com/octocat/Hello-World.git","ref":"master","refresh":false},"prompt":"在 README 中增加一行 RUNNER_TEST_OK"}'
```

收到 `202` 和 `task_id` 后，使用返回的 `links.events` 连接 SSE：

```bash
curl -N -H 'Accept: text/event-stream' \
  http://127.0.0.1:18080/v1/tasks/<task_id>/events
```

事件包含递增的 `id`、`event` 和 JSON `data`；断线后把最后收到的数字 `id` 放入 `Last-Event-ID` 请求头即可续读。空闲时每 15 秒发送 heartbeat，任务结束时发送 `task.completed` 或 `task.failed` 并关闭流。Manager 端口仅绑定服务器回环地址。本阶段任务状态由本机文件保存，Manager 在运行中重启时尚无任务恢复机制。

任务结束后读取 `links.result`：

```bash
curl -sS http://127.0.0.1:18080/v1/tasks/<task_id>/result
```

任务尚在运行时返回 `202` 和 `Retry-After: 2`；结束后返回 `200`，包含 `status`、`exit_code`、`final_message`、`diff`、缓存命中情况、分段耗时和产物下载链接。可下载的产物为 `changes.diff` 与 `codex-events.jsonl`。

Runner 已在 ARM64 服务器上通过一次性容器完成源码执行验证：从 Git 仓库克隆任务代码、调用 Codex 修改 `README.md`、导出 JSONL 事件和 diff，然后自动删除容器。Runner 使用 Docker 容器作为隔离边界，容器内不再启动嵌套的 bubblewrap 沙箱。

讯飞 MaaS 的 `xopglm53` 同时提供 Responses 接口。Runner 已通过 `https://maas-api.cn-huabei-1.xf-yun.com/v1/responses` 完成直接调用及文件修改测试，无需协议转换层。`runner/config.toml` 的 `base_url` 配置为 `/v1`，由 Codex 自动请求 `/responses`；API 密钥通过 Runner 环境变量 `MODEL_API_KEY` 注入，不写入仓库或镜像。

## Runner 生命周期

```text
API Manager
-> Docker Engine创建Runner
-> Runner执行codex exec --json
-> API Manager转发事件
-> 结果写入宿主机任务目录
-> 删除Runner容器及其内部工作目录
```

## Codex

上游项目：<https://github.com/openai/codex>
