# Codex Sandbox MVP

公开实验仓库，用两个 ARM64 镜像验证持久对话工作区与一次性 Codex Runner 闭环：

- `codex-api-manager`：常驻 API/SSE 与 Docker 生命周期管理服务。
- `codex-runner`：每个任务创建一个，任务结束后销毁。

## 构建

推送 `main` 或 `v*` 标签后，GitHub Actions 在原生 ARM64 Runner 上构建并推送：

```text
ghcr.io/<owner>/codex-api-manager:latest
ghcr.io/<owner>/codex-runner:latest
```

Codex 源码通过 `vendor/codex` Git Submodule 固定到具体提交。
Runner 构建时应用 [skill-loaded.patch](runner/patches/skill-loaded.patch)，在 Codex 确实把 Skill 指令加入本轮上下文后发出可核验的运行时标记。前端内置 `markdown-it` 15.0.2 与 `DOMPurify` 3.4.15，许可文件位于 `web/vendor/` 对应目录。

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

API Manager 支持 `POST /v1/tasks` 创建一次性 Runner；`GET /v1/tasks/{task_id}` 查询状态；`GET /v1/tasks/{task_id}/events` 实时推送 Codex 事件；`GET /v1/tasks/{task_id}/result` 返回最终回答、退出码和 diff；`GET /v1/capacity` 查看单机并发。Runner 完成或超时后由 Manager 删除，结果文件暂存于 `/data/codex-mvp/results/{task_id}`。

## Skill 广场与用户目录

API Manager 首次启动时把仓库 [skill-catalog/public](skill-catalog/public) 中的五项已审核 Skill 复制到宿主机 `/data/codex-mvp/skills/public/`。来源、固定提交和许可见 [SOURCES.md](skill-catalog/SOURCES.md)。这个公共目录由管理员维护；启动时的初始化只执行一次，后续不会覆盖管理员修改或删除的 Skill。每个子目录必须包含名称与目录 ID 一致的 `SKILL.md`，可附带 `references/`、`assets/` 和许可文件。当前广场不接受用户提交任意 GitHub URL。

安装时服务端复制整个 Skill 目录到 `/data/codex-mvp/skills/users/<用户ID的SHA-256>/skills/<skill-id>/`；卸载只删除该用户目录下的副本。安装状态直接来自文件系统，不增加数据库表。用户 ID 由现有 API Key 鉴权确定；若使用 `local-dev` 模式，所有访问仍属于同一用户。

| 接口 | 作用 |
| --- | --- |
| `GET /v1/skills/catalog` | 读取公共目录，附当前用户的安装状态 |
| `GET /v1/skills` | 列出当前用户已安装的 Skill |
| `POST /v1/skills/{skill-id}/install` | 从公共目录复制到当前用户目录；重复安装保持原副本 |
| `DELETE /v1/skills/{skill-id}` | 删除当前用户目录中的 Skill，不影响公共目录或其他用户 |

例如，当前用户安装后可在前端选择“用于提问”，输入框会填入 `$awesome-code-review`。创建任务时，Manager 把该用户当时已安装的 Skill 复制为任务快照，只读挂载到 Runner 的 `/home/codex/.agents/skills`；任务结束后清理快照。这样安装或卸载只影响之后创建的任务。当前问题显式写出 `$skill-id` 时，Codex 自行选择并加载该 Skill 的 `SKILL.md`；Manager 不拼接全文。历史轮次里的 `$skill-id` 会转换为不可触发选择的展示字符。不显式指定时，Codex 仍可根据 Skill 描述自行选择并读取。任务状态和结果的 `skills` 表示**本轮可用**，`requested_skills` 表示**本轮显式指定**；Codex 原生加载成功时 Runner 发出 `skill.loaded`，通过命令完整读取文件时发出独立的 `skill.read`。结果中的 `loaded_skills` 与 `read_skills` 分别对应这两种证据，均不保证模型完全遵循每条指令。每位用户最多安装 20 项，每项最大 1 MB；所有公共 Skill 须由管理员先检查内容和依赖。

修改公共目录里的 Skill 不需要重新构建镜像，但已安装的用户副本不会自动更新；需要用户卸载后重新安装。若要更新仓库附带的首批公共 Skill，需要更新仓库并重新构建 API Manager 镜像，再由管理员决定如何更新服务器上的公共目录。

## MCP 广场与 Codex 原生加载

MCP 广场首批包含六个经过 `initialize`、`tools/list` 和代表性 `tools/call` 检查的远程 Streamable HTTP 服务：Context7、arXiv、Wikipedia、Weather、Qt Docs 和 Vonage Docs。公共目录由 `api-manager/app/mcp_store.py` 中的审核清单维护；用户安装状态保存于数据库表 `user_mcp_installations`，不创建用户 MCP 文件目录。

| 接口 | 用途 |
| --- | --- |
| `GET /v1/mcp/catalog` | 获取公共 MCP，并附当前用户安装状态 |
| `GET /v1/mcp` | 获取当前用户已安装的 MCP |
| `POST /v1/mcp/{mcp-id}/install` | 安装一个已审核 MCP |
| `DELETE /v1/mcp/{mcp-id}` | 卸载当前用户的 MCP |

创建任务时，Manager 查询该用户当时已安装的 MCP，把原始 Runner 配置复制为任务配置，并追加原生 `[mcp_servers."..."]` 表。该文件只读挂载到 Runner 的 `/home/codex/.codex/config.toml`，任务结束后删除。Codex 启动后自行执行 MCP `initialize`、`tools/list` 和 `tools/call`，并通过 Responses API 的结构化工具字段把工具提供给模型；Manager 不向用户 Prompt 追加 MCP 名称、说明或调用规则。

安装状态只影响之后创建的任务。运行时原生 `mcp_tool_call` 的开始、完成和失败状态会随 Codex JSONL 进入现有 SSE，前端在执行进度中直接展示。公共 Registry 里的条目不会自动发布到广场；新增条目应先核对来源、认证方式、工具权限、连接状态和实际调用结果。

## 多窗口、持久工作区与五轮上下文

对话元数据持久化在 PostgreSQL。一个用户可拥有多个对话窗口，每个新窗口在 `/data/codex-mvp/workspaces/<用户哈希>/<conversation-id>/` 拥有独立空白工作区。同一窗口的每轮任务都会创建新的 Runner，但以读写方式挂载同一工作区，所以第一轮创建的文件能在后续轮次继续读取和修改；不同窗口的目录彼此隔离。历史问答完整保留并分页读取；每次执行只从**当前窗口最近五轮成功问答**选取上下文。失败任务显示在历史中，但不计入模型上下文。超出 `MAX_HISTORY_BYTES` 的上下文会从最旧的一整轮开始舍弃。

Runner 仍使用 `codex exec --ephemeral`：Codex 自身不保存会话，交流上下文由 Manager 提供，文件状态由持久工作区提供。工作区内部使用本地 Git 检查点记录每轮文件变化，不连接远程仓库。旧的 `POST /v1/tasks` 继续支持 Git 仓库快照模式，用于单次任务和兼容测试。

新增接口：

| 接口 | 作用 |
| --- | --- |
| `GET /v1/me` | 返回当前用户标识 |
| `POST /v1/conversations` | 创建带独立空白工作区的新窗口 |
| `GET /v1/conversations?limit=50&offset=0` | 分页列出当前用户的窗口摘要 |
| `GET /v1/conversations/{id}` | 读取窗口信息和运行中的任务 ID |
| `GET /v1/conversations/{id}/turns?limit=20&before_seq=N` | 向前分页读取该窗口完整历史 |
| `POST /v1/conversations/{id}/turns` | 提交当前问题，返回 `turn_id`、`task_id`、事件与结果链接 |
| `GET /v1/conversations/{id}/files` | 浏览当前窗口工作区文件 |
| `GET /v1/conversations/{id}/files/content?path=...` | 下载工作区中的单个文件 |
| `GET /v1/conversations/{id}/workspace` | 下载排除构建缓存与内部 Git 的工作区压缩包 |
| `DELETE /v1/conversations/{id}` | 删除窗口、历史、任务结果和工作区 |

提交示例：`{"message":"继续解释上一条回答","request_id":"client-generated-id"}`。`request_id` 用于安全重试，不能在同一窗口复用到不同问题。每个窗口同时只允许一个运行中的任务；其他窗口仍受 `MAX_ACTIVE_TASKS` 全局限制。原有单次任务接口和 Runner 创建、销毁流程保持不变。

`USER_API_KEYS_JSON` 可配置用户与 API Key。在 `.env` 中写入 `USER_API_KEYS_JSON='{"alice":"replace-with-random-key","bob":"replace-with-another-key"}'`，并设 `ALLOW_LOCAL_DEV_USER=0`。配置后，所有对话和任务接口要求 `Authorization: Bearer <key>`，任务事件、结果和产物也按所有者校验。未配置时仅用于本地实验，所有请求属于 `local-dev`。浏览器输入的 Key 只保存在本地 Web 服务进程的短时会话中，浏览器只收 HttpOnly Cookie。

新窗口不继承其他窗口的对话或文件。每轮仍使用新的 Runner；Runner 结束后容器被删除，窗口工作区继续保留。一个窗口同时只运行一轮，避免两个 Runner 并发写入同一目录。

## 本地前端原型

无需安装前端依赖，在本机运行：

```bash
python3 web/server.py
```

打开 <http://127.0.0.1:5173>。左侧从服务端加载用户的对话窗口；打开窗口后分页读取完整历史，中间显示多轮提问与回答，运行时把可读推理摘要与命令、工具、Skill 加载和 MCP 调用等执行事件分区展示，右侧可查看每轮代码变更。模型未返回摘要时页面明确显示“未提供”，不会编造内部推理。回答和推理摘要使用随页面一起提供的 Markdown 渲染器，并清理不安全的 HTML；不依赖外部 CDN。刷新会恢复当前窗口及其历史。新对话在第一次提问时创建，并同时分配独立空白工作区。顶部工作区入口可以浏览和下载当前窗口文件。技能广场和 MCP 广场均支持按用户安装与卸载；知识库仍是预留入口。页面提交时只发送当前问题，最近五轮上下文由 API Manager 从数据库读取。

如果 API Manager 在远程服务器上且只监听其回环地址，正常情况下先在另一个本机终端建立 SSH 转发：

```bash
ssh -L 18080:127.0.0.1:18080 root@<server-ip>
```

前端代理默认访问本机 `http://127.0.0.1:18080`。需要改端口时可设置 `CODEX_API_BASE` 和 `CODEX_WEB_PORT`。前端服务器本身只监听 `127.0.0.1`。

当前实验服务器禁止 SSH TCP 转发，可改用本地 SSH 命令通道。它不会改动服务器配置；密码只在本地进程内使用，不写入项目：

```bash
python3 -m pip install -r web/requirements.txt
CODEX_SSH_HOST=172.29.231.119 CODEX_SSH_USER=root python3 web/server.py
```

启动时输入 SSH 密码。`CODEX_REMOTE_API_PORT` 默认 `18080`，可用于测试其他端口。每次提问仍会创建新的任务；页面刷新后从数据库恢复完整历史。知识库尚未接入。

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
