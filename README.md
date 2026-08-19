# ZSvirt MCP Server

让 AI 能够动态查询和调用 ZSvirt 的 2000+ API 的 MCP Server。

## 功能特性

- **API 搜索**: 根据关键词搜索 ZStack API，支持模糊匹配
- **API 描述**: 获取 API 的详细参数说明
- **API 执行**: 执行 ZStack API 并返回结果
- **监控指标搜索**: 搜索可用的监控指标
- **监控数据获取**: 获取指定指标的监控数据

## 安装

```bash
# 从 PyPI 安装
pip install zsvirt-mcp-server

# 或者使用 uv
uv pip install zsvirt-mcp-server
```

> 💡 也可以不安装，直接用 `uvx` 或 `pipx run` 一键运行（见下方使用方式）

## 配置

设置以下环境变量：

```bash
export ZSTACK_API_URL="http://localhost:8080"  # ZStack API 地址
export ZSTACK_ALLOW_ALL_API="false"             # 是否允许写操作（可选，默认 false）

# 认证方式一：用户名密码（会自动登录获取 Session）
export ZSTACK_ACCOUNT="admin"                   # 账户名
export ZSTACK_PASSWORD="your-password"          # 密码（明文）

# 认证方式二：直接传入 SessionID（优先级更高，设置后忽略用户名密码）
export ZSTACK_SESSION_ID="your-session-uuid"    # 已有的 Session UUID

# 查询响应控制（可选）
export ZSTACK_QUERY_DEFAULT_LIMIT="50"          # Query API 默认 limit（设 0 禁用）
export ZSTACK_RESPONSE_SIZE_LIMIT="65536"       # 响应大小上限，字节（设 0 禁用）
```

### 认证方式说明

| 方式 | 环境变量 | 说明 |
|------|----------|------|
| 用户名密码 | `ZSTACK_ACCOUNT` + `ZSTACK_PASSWORD` | 自动登录获取 Session |
| Session ID | `ZSTACK_SESSION_ID` | 直接使用已有 Session（优先级更高） |

> 💡 如果同时设置了 `ZSTACK_SESSION_ID` 和用户名密码，会优先使用 Session ID

### 安全说明

**默认情况下，只允许调用只读 API**，包括：
- `Query*` - 查询类
- `Get*` - 获取类  
- `List*` - 列表类
- `Describe*` - 描述类
- `Check*` - 检查类
- `Count*` - 计数类
- 其他只读操作...

如需调用写操作 API（如 `CreateVmInstance`、`DeleteVolume` 等），需要设置：
```bash
export ZSTACK_ALLOW_ALL_API="true"
```

⚠️ **警告**: 启用写操作后，AI 可以执行创建、删除、修改等危险操作，请谨慎使用！

### 查询响应控制

Query API 默认注入 `limit=50`，防止一次拉取全量数据撑满模型上下文窗口。响应超过 64KB 时会自动裁剪 inventories 列表，保证返回合法 JSON。

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `ZSTACK_QUERY_DEFAULT_LIMIT` | `50` | Query API 未指定 limit 时自动注入的默认值，设 `0` 禁用 |
| `ZSTACK_RESPONSE_SIZE_LIMIT` | `65536` | 响应大小上限（字节），超过后裁剪，设 `0` 禁用 |

- 显式传入 `limit` 时不会被覆盖
- 裁剪发生时响应中会包含 `_truncation` 字段，提示使用 `limit`/`start` 翻页或 `fields` 精简返回字段

## 使用方式

### 作为 MCP Server 运行

```bash
# 使用 uvx 直接运行（无需安装）
uvx zsvirt-mcp-server

# 或使用 pipx
pipx run zsvirt-mcp-server

# 如果已安装，直接运行
zsvirt-mcp-server
```

### SSE 模式运行

默认使用 stdio 传输。若需 SSE 模式，可用命令行或环境变量切换：

```bash
# 命令行方式
uvx zsvirt-mcp-server --transport sse --host 0.0.0.0 --port 8000

# 环境变量方式
export MCP_TRANSPORT="sse"
export MCP_HOST="0.0.0.0"
export MCP_PORT="8000"
export MCP_PATH="/sse"  # 可选
uvx zsvirt-mcp-server
```

> 说明：也兼容 `FASTMCP_HOST` / `FASTMCP_PORT` / `FASTMCP_MOUNT_PATH`（FastMCP 原生环境变量）

### Streamable HTTP 模式运行

```bash
# 命令行方式
uvx zsvirt-mcp-server --transport streamable-http --host 0.0.0.0 --port 8000 --streamable-path /mcp

# 环境变量方式
export MCP_TRANSPORT="streamable-http"
export MCP_HOST="0.0.0.0"
export MCP_PORT="8000"
export MCP_STREAMABLE_PATH="/mcp"  # 可选
uvx zsvirt-mcp-server
```

> 说明：也兼容 `FASTMCP_STREAMABLE_HTTP_PATH`

### HTTP 头认证（多租户模式）

在 SSE 或 streamable-http 模式下，管理员可以启动一个共享的 MCP Server，多个用户通过 HTTP 头传入各自的凭据，实现多租户隔离。

支持的 HTTP 头：

| HTTP Header | 对应环境变量 | 说明 |
|---|---|---|
| `X-ZStack-Account` | `ZSTACK_ACCOUNT` | 账户名 |
| `X-ZStack-Password` | `ZSTACK_PASSWORD` | 密码 |
| `X-ZStack-Session-Id` | `ZSTACK_SESSION_ID` | 已有 Session（优先级高于账号密码） |
| `X-ZStack-API-URL` | `ZSTACK_API_URL` | ZStack 管理节点地址（可代理多套环境） |

凭据优先级：HTTP 头 > 环境变量

典型用法：
```bash
# 管理员启动共享 MCP Server
ZSTACK_ALLOW_ALL_API=false uvx zsvirt-mcp-server --transport streamable-http --host 0.0.0.0 --port 8000
```

用户在 MCP 客户端配置中添加 HTTP 头即可使用各自的账号：
```json
{
  "mcpServers": {
    "zstack": {
      "transport": "streamable-http",
      "url": "http://mcp-server:8000/mcp",
      "headers": {
        "X-ZStack-Account": "user-a",
        "X-ZStack-Password": "password-a",
        "X-ZStack-API-URL": "http://zstack-env-1:8080"
      }
    }
  }
}
```

特性：
- 同一账号的 Session 会自动缓存复用，不会每次请求都创建新 Session
- 不同 `X-ZStack-API-URL` 的请求会路由到不同的 ZStack 环境
- stdio 模式下无 HTTP 头，自动回退到环境变量认证，行为不变

### 在 Claude Desktop 中配置

在 `claude_desktop_config.json` 中添加：

**方式一：使用用户名密码**
```json
{
  "mcpServers": {
    "zstack": {
      "command": "uvx",
      "args": ["zsvirt-mcp-server"],
      "env": {
        "ZSTACK_API_URL": "http://your-zstack-server:8080",
        "ZSTACK_ACCOUNT": "admin",
        "ZSTACK_PASSWORD": "your-password",
        "ZSTACK_ALLOW_ALL_API": "false"
      }
    }
  }
}
```

**方式二：使用 Session ID**
```json
{
  "mcpServers": {
    "zstack": {
      "command": "uvx",
      "args": ["zsvirt-mcp-server"],
      "env": {
        "ZSTACK_API_URL": "http://your-zstack-server:8080",
        "ZSTACK_SESSION_ID": "your-session-uuid",
        "ZSTACK_ALLOW_ALL_API": "false"
      }
    }
  }
}
```

> 💡 将 `ZSTACK_ALLOW_ALL_API` 设为 `"true"` 可启用写操作（创建/删除/修改等）

## 可用工具

### 1. search_api

根据关键词搜索 ZStack API。

**参数**:
- `keywords` (list[str]): 搜索关键词，如 `["Query", "Vm"]`
- `category` (str, 可选): 按分类过滤
- `limit` (int, 默认 15): 最多返回数量

### 2. describe_api

获取指定 API 的详细参数说明。

**参数**:
- `api_name` (str): API 名称，如 `"QueryVmInstance"`

### 3. execute_api

执行 ZStack API。

**参数**:
- `api_name` (str): API 名称
- `parameters` (dict): API 参数

### 4. search_metric

搜索可用的监控指标。

**参数**:
- `keywords` (list[str]): 搜索关键词
- `namespace` (str, 可选): 按命名空间过滤（支持模糊匹配，如 `vm`/`host`）
- `limit` (int, 默认 20): 最多返回数量
- `match_mode` (str, 默认 `or`): 关键词匹配模式（`and`/`or`）
- `prefer_namespaces` (list[str], 可选): 优先排序的命名空间列表（默认 `["ZStack/VM","ZStack/Host"]`）

> 💡 提示：不确定 namespace 时可先不传，返回结果会带 namespace 值供选择
> 💡 默认 `match_mode=or`（多关键词并集）；如需交集请显式传 `and`
> 💡 指标名称在不同 namespace 可能重名，建议指定 `namespace` 或 `prefer_namespaces` 以确保排序优先

### 5. get_metric_data

获取监控数据。

**参数**:
- `namespace` (str): 命名空间
- `metric_name` (str): 指标名称
- `start_time` (str|int, 可选): 开始时间 (ISO 或秒级时间戳)
- `end_time` (str|int, 可选): 结束时间 (ISO 或秒级时间戳)
- `period` (int, 默认 60): 采样周期(秒)
- `labels` (list[str]|dict, 可选): 标签过滤，如 `["VMUuid=xxx"]` 或 `{"VMUuid":"xxx"}`
- `summary_only` (bool, 可选): 仅返回统计信息（点数/最大/最小/平均/方差/标准差）

**数据量提示**:
- 返回点数估算：`ceil((end_time - start_time) / period) * series_count`
- `series_count` 为不同 label 组合数量；不传 `labels` 时可能返回多组序列
- 建议通过缩短时间范围、增大 `period` 或增加 `labels` 过滤避免输出过大

### 6. get_metric_summary

获取监控指标的聚合 TopN（按 label_key 分组）。

**参数**:
- `namespace` (str): 命名空间
- `metric_name` (str): 指标名称
- `label_key` (str): 标签键，如 `VMUuid`/`HostUuid`
- `metric_names` (list[str], 可选): 多指标合并（如 in/out）
- `start_time` (str|int, 可选): 开始时间 (ISO 或秒级时间戳)
- `end_time` (str|int, 可选): 结束时间 (ISO 或秒级时间戳)
- `period` (int, 默认 60): 采样周期(秒)
- `aggregate` (str, 默认 max): 单指标聚合方式 (max/avg/sum/min)
- `combine` (str, 默认 sum): 多指标合并方式 (sum/avg/max/min)
- `threshold_op` (str, 可选): 阈值比较符 (>,>=,<,<=,==,!=)
- `threshold_value` (number, 可选): 阈值数值
- `top_n` (int, 默认 10): 返回条数
- `resolve_resource` (str, 可选): `vm` 或 `host`，用于解析名称

## Query API 条件语法

对于 Query 类 API，`conditions` 参数支持以下操作符：

| 操作符 | 含义 | 示例 |
|--------|------|------|
| `=` | 等于 | `name=test` |
| `!=` | 不等于 | `state!=Deleted` |
| `>` | 大于 | `cpuNum>4` |
| `>=` | 大于等于 | `memorySize>=1073741824` |
| `<` | 小于 | `createDate<2024-01-01` |
| `<=` | 小于等于 | |
| `?=` | 模糊匹配(LIKE，部分版本为 `like`) | `name?=%test%` |
| `!?=` | 模糊不匹配 | |
| `~=` | 正则匹配 | `name~=.*test.*` |
| `!~=` | 正则不匹配 | |
| `=null` | 为空 | `description=null` |
| `!=null` | 不为空 | |
| `in` | 在列表中 | `state?=Running,Stopped` |
| `not in` | 不在列表中 | `state!?=Deleted,Destroyed` |

**conditions 格式**:
```json
{
    "conditions": [
        {"name": "uuid", "op": "=", "value": "xxx"},
        {"name": "state", "op": "in", "value": "Running,Stopped"}
    ]
}
```

## 示例交互

用户问: "帮我查一下 UUID 为 ae6e57a0 开头的 VM 的详情"

AI 会:
1. 调用 `search_api(keywords=["Query", "Vm", "Instance"])`
2. 调用 `describe_api(api_name="QueryVmInstance")`
3. 调用 `execute_api(api_name="QueryVmInstance", parameters={"conditions": [{"name": "uuid", "op": "?=", "value": "ae6e57a0%"}]})`

## 开发

```bash
# 克隆仓库
git clone https://github.com/ZSvirt/zsvirt-mcp-server/zsvirt-mcp-server.git
cd zsvirt-mcp-server

# 安装开发依赖
pip install -e ".[dev]"

# 运行测试
pytest
```

## License

MIT

