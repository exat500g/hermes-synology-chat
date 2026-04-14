# Synology Chat Adapter for Hermes Agent

将 Synology Chat 集成为 Hermes Agent 的原生平台适配器，通过 Webhook 接收消息、通过 External Chat API 发送回复。

## 架构

```
Synology Chat ──webhook POST──► aiohttp server (adapter)
                                    │
                                    ▼
                              Hermes Agent
                                    │
                                    ▼
External Chat API ◄──POST──── send() 
```

| | OpenClaw Bot | Hermes Adapter |
|---|---|---|
| 入站消息 | Flask webhook | aiohttp webhook |
| 出站回复 | External API | External API |
| AI 后端 | WebSocket → OpenClaw | 内置 → Hermes Agent |
| 进程模型 | 独立 Python 进程 | Hermes Gateway 子进程 |

## 功能

- ✅ Webhook 接收消息（form POST）
- ✅ External Chat API 发送回复
- ✅ Token 验证（HMAC）
- ✅ 幂等去重（防止 Synology 重试导致重复处理）
- ✅ 速率限制（默认 30 次/分钟）
- ✅ 用户授权白名单（环境变量）
- ✅ 健康检查端点 (`GET /health`)
- ✅ 毫秒时间戳自动归一化
- ✅ 异常时间戳容错处理

## 集成到 Hermes Agent

需要修改 Hermes Agent 的 3 个文件：

### 1. `gateway/config.py` — 添加 Platform 枚举

在 `class Platform(Enum)` 中添加：

```python
SYNOLOGY_CHAT = "synology_chat"
```

### 2. `gateway/run.py` — 添加适配器工厂 + 授权

**适配器工厂** (`_create_adapter` 方法，其他 platform 的 elif 块附近)：

```python
elif platform == Platform.SYNOLOGY_CHAT:
    from gateway.platforms.synology_chat import SynologyChatAdapter, check_synology_chat_requirements
    if not check_synology_chat_requirements():
        logger.warning("Synology Chat: aiohttp not installed. Run: pip install aiohttp")
        return None
    return SynologyChatAdapter(config)
```

**用户授权** — 在 `_is_user_authorized` 方法中：
- `platform_allowlist_map` 添加: `Platform.SYNOLOGY_CHAT: "SYNOLOGY_CHAT_ALLOWED_USERS"`
- `platform_allow_all_map` 添加: `Platform.SYNOLOGY_CHAT: "SYNOLOGY_CHAT_ALLOW_ALL_USERS"`
- 环境变量列表中添加 `"SYNOLOGY_CHAT_ALLOWED_USERS"` 和 `"SYNOLOGY_CHAT_ALLOW_ALL_USERS"`

### 3. `hermes_cli/platforms.py` — 注册平台

在 `PLATFORMS` OrderedDict 中添加：

```python
("synology_chat",  PlatformInfo(label="🏠 Synology Chat",   default_toolset="hermes-synology-chat")),
```

### 4. 复制适配器文件

```bash
cp synology_chat.py ~/.hermes/hermes-agent/gateway/platforms/synology_chat.py
```

## 配置 (`~/.hermes/config.yaml`)

```yaml
platforms:
  synology_chat:
    enabled: true
    token: "your_synology_chat_bot_token"
    extra:
      host: "0.0.0.0"
      port: 8086
      api_endpoint: "https://your-nas-ip:5001/webapi/entry.cgi"
      ssl_verify: false
      webhook_path: "/synology-chat/webhook"
```

| 字段 | 说明 | 默认值 |
|------|------|--------|
| `token` | 机器人令牌（必填） | — |
| `extra.host` | 监听地址 | `0.0.0.0` |
| `extra.port` | 监听端口 | `8086` |
| `extra.api_endpoint` | External API 地址 | `https://127.0.0.1:5001/webapi/entry.cgi` |
| `extra.ssl_verify` | 验证 SSL 证书 | `false` |
| `extra.webhook_path` | Webhook 路径 | `/synology-chat/webhook` |

## 用户授权

在 `~/.hermes/.env` 中配置：

```bash
# 允许特定用户（逗号分隔）
SYNOLOGY_CHAT_ALLOWED_USERS=123,456,789

# 或允许所有用户
SYNOLOGY_CHAT_ALLOW_ALL_USERS=true
```

## Synology Chat 侧配置

1. **创建机器人**：DSM → Chat 应用 → 右上角头像 → 整合 → 机器人 → 创建
2. **配置传出 URL**（webhook 地址）：`http://<hermes-server-ip>:8086/synology-chat/webhook`
3. **权限**：给机器人账号开 Chat 权限

## 测试

```bash
# 健康检查
curl http://localhost:8086/health

# 模拟 webhook POST
curl -X POST http://localhost:8086/synology-chat/webhook \
  -d "token=your_bot_token" \
  -d "user_id=123" \
  -d "username=testuser" \
  -d "text=你好" \
  -d "timestamp=$(date +%s)"
```

## 已知限制

- Synology Chat 不支持文件发送（Synology 侧限制）
- 不支持群组 @提及（需 Synology webhook 支持）
- 不支持富文本/卡片消息，仅纯文本

## 依赖

- `aiohttp`（Hermes messaging extras 已包含）

## License

MIT
