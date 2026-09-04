# Railway Universal Stable Deployment V5.6

这是一个面向 Railway 的 Xray 稳定部署项目。核心原则是：**节点身份只初始化一次，之后永久复用；运行时网络 endpoint 随当前 Deployment 重新发现。**

## 1. 部署方式

1. 将 GitHub 仓库连接到 Railway Service。
2. Service 使用仓库中的 `Dockerfile`。
3. 在 **Service → Volumes** 创建 Persistent Volume，并将 **Mount Path 固定为 `/data`**。
4. 在 **Service → Variables** 人工设置 `RAILWAY_TOKEN`（项目级 Railway API Token）。
5. Deploy。
6. 首次启动先验证 `/data` 是真实挂载的 Persistent Volume；仅当该 Volume 真正为空时，初始化 UUID、REALITY key、3 个 Short IDs 和 subscription token，并原子写入 `/data`。
7. 程序自动检查/创建当前 Deployment 所需的 Public Domain 与 TCP Proxy（Target `8080`）；网络资源发生创建/整理时请求 Railway Redeploy。
8. 后续 Restart / Redeploy / Container recreation 均从 `/data` 读取并复用同一套节点身份；runtime manifest、订阅和 Railway endpoint 根据当前 Deployment 环境重新生成。

**Volume 必须在首次正式 Deploy 前完成挂载。** 程序不会把临时容器文件系统中的身份当作永久身份，也不会在缺少 Volume 时生成一套以后无法可靠保留的身份。

## 2. 节点

- Node 1：VLESS XHTTP TLS
- Node 2：VLESS RAW REALITY Vision
- Node 3：VLESS XHTTP REALITY
- Node 4：VLESS gRPC REALITY
- Node 06：Cloudflare Tunnel + VLESS XHTTP TLS

## 3. 永久身份策略

节点身份唯一持久来源为 `/data`：

- `uuid.txt`
- `reality_private_key.txt`
- `reality_public_key.txt`
- `reality_short_ids.json`
- `subscription_token.txt`
- `identity-integrity.json`
- `.node-identity-initialized`

策略固定为 **`INITIALIZE_ONCE_REUSE_FOREVER`**：

- **空 Persistent Volume**：初始化一次。
- **已初始化且完整有效、完整性校验通过**：输出 `NODE_IDENTITY=REUSED`。
- **已初始化但身份文件缺失、损坏、不完整或完整性校验失败**：拒绝启动，**绝不生成新身份**。
- **未挂载 Persistent Volume**：拒绝启动，**绝不生成临时身份**。
- `generate.py` 只读取 Short IDs，不负责生成身份。

`identity-integrity.json` 保存身份文件的 SHA-256 完整性封印，用于阻止一个格式仍然有效、但已经被人为修改的 UUID、REALITY key、Short ID 或 subscription token 被静默当成原身份继续运行。

## 4. 运行时网络与身份的边界

Railway Public Domain、TCP Proxy domain/port 属于当前 Deployment 的运行时环境，**不属于节点身份**。每次启动以当前 Railway 环境为 endpoint 权威来源，因此 endpoint 可以变化，而 UUID / REALITY key / Short IDs / subscription token 不变。

`RAILWAY_TOKEN` 只用于 Railway API 网络资源 bootstrap。程序不会通过 Railway API 修改节点身份、国家/地区或用户未授权的外部配置。

## 5. 启动与故障恢复

主进程由 `boot.sh` 直接运行。启动顺序为：

`identity preflight → Gateway :8080 → Railway networking reconciliation → runtime discovery → runtime/subscription generation → Xray → optional Cloudflare Tunnel → readiness`

Gateway 或 Xray 主进程异常退出时，容器以失败状态结束，由 Railway `ON_FAILURE` 负责重新启动。重新启动后仍从 `/data` 复用同一身份。

Railway healthcheck 使用 `/health` 作为早期 liveness 检查；`boot.sh` 在完成 runtime 和 Xray readiness 检查后保持主进程运行。

## 6. 仓库结构

```text
.
├── Dockerfile
├── railway.toml
├── README.md
├── .dockerignore
├── .editorconfig
├── config/
│   └── reality-sni-candidates.txt
├── docs/
│   └── identity-policy.md
├── scripts/
│   ├── boot.sh
│   ├── gateway.py
│   ├── generate.py
│   ├── identity-init.py
│   ├── railway_setup.py
│   ├── runtime-manifest.py
│   └── version.py
└── site/
    └── index.html
```

`supervise.sh` 已移除。当前架构不再通过第二层 shell supervisor 包裹 `boot.sh`，避免 PID1 生命周期、exit code 10（Railway redeploy handoff）和重启语义发生冲突；Railway 原生 `ON_FAILURE` 负责容器级恢复。

## 7. 构建保护

Docker build 阶段会检查：

- Xray 版本与镜像 digest 固定；
- Python 脚本可编译；
- Persistent Volume guard、身份复用、fail-closed、integrity seal 存在；
- `boot.sh` 必须先执行身份初始化；
- Short IDs 必须从持久身份读取；
- Railway Public Domain / TCP Proxy 必须执行数量与 target `8080` 的 invariant 检查；
- WS transport 已作为 Node 02 正式启用；运行时身份生成逻辑仍禁止在 generate.py 中执行。

## 8. 验收标准

首次部署应出现：

```text
PERSISTENT_VOLUME=/data
PERSISTENT_VOLUME_MOUNT=PASS
NODE_IDENTITY=INITIALIZED
NODE_IDENTITY_FINGERPRINT=<fingerprint>
```

后续重启/重新部署应出现：

```text
PERSISTENT_VOLUME=/data
PERSISTENT_VOLUME_MOUNT=PASS
NODE_IDENTITY=REUSED
NODE_IDENTITY_FINGERPRINT=<same fingerprint>
```

同一 Persistent Volume 的 fingerprint 必须保持不变；身份完整性封印必须保持有效。

## 9. 生产原则

- 不删除或清空 `/data`，除非明确执行一次全新节点初始化。
- 不把 UUID、REALITY key、Short IDs 或 subscription token 写入 Git 仓库。
- 不在运行时重新生成节点身份。
- 不把 Railway endpoint 固化进身份文件。
- 不修改 Railway 国家/地区、Node 6 或其他用户未授权设置。
