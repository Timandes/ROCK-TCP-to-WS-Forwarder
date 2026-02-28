# tcp2ws

TCP 到 WebSocket 的转发服务，支持持久化连接和自动重连。

## 安装

```bash
cd tcp2ws
uv sync
```

## 使用

```bash
# 转发到沙箱
uv run main.py <sandbox_id> -p 8888 -w 8080

# 本地转发（无沙箱ID）
uv run main.py -p 8888 -w 8080
```

### 参数

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `sandbox_id` | 沙箱 ID（可选） | - |
| `-p, --port` | TCP 监听端口 | 8888 |
| `-w, --ws-port` | WebSocket 服务端口 | 8080 |

## 功能

- **持久化连接**：启动时建立 WebSocket 连接，失败则退出
- **自动重连**：连接断开后自动重连，连续失败 3 次退出
- **沙箱检测**：检测到沙箱未启动时直接退出
- **双向转发**：TCP 和 WebSocket 之间双向透明转发二进制数据

## WebSocket URL 格式

- 有沙箱：`ws://localhost:{ws_port}/apis/envs/sandbox/v1/sandboxes/{sandbox_id}/portforward?port=9999`
- 无沙箱：`ws://localhost:{ws_port}/portforward?port=9999`
