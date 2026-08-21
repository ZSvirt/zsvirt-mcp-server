ZSvirt MCP Server

**English** | [简体中文](./README.zh-CN.md)

An MCP server that enables AI assistants to dynamically discover and invoke more than 2,000 ZSvirt APIs.

## Features

- **API search**: Search ZStack APIs by keyword with fuzzy matching
- **API description**: Retrieve detailed parameter documentation for an API
- **API execution**: Invoke a ZStack API and return its result
- **Metric search**: Search available monitoring metrics
- **Metric data retrieval**: Retrieve monitoring data for a specified metric

## Installation

```bash
# Install from PyPI
pip install zsvirt-mcp-server

# Or install with uv
uv pip install zsvirt-mcp-server
```

> 💡 You can also run the server directly with `uvx` or `pipx run` without installing it. See [Usage](#usage).

## Configuration

Set the following environment variables:

```bash
export ZSTACK_API_URL="http://localhost:8080"  # ZStack API endpoint
export ZSTACK_ALLOW_ALL_API="false"             # Allow write operations (optional; default: false)

# Authentication method 1: account and password (automatically logs in and obtains a session)
export ZSTACK_ACCOUNT="admin"                   # Account name
export ZSTACK_PASSWORD="your-password"          # Plain-text password

# Authentication method 2: use an existing session ID
# This method takes precedence over account/password authentication.
export ZSTACK_SESSION_ID="your-session-uuid"    # Existing session UUID

# Query response controls (optional)
export ZSTACK_QUERY_DEFAULT_LIMIT="50"          # Default Query API limit; set to 0 to disable
export ZSTACK_RESPONSE_SIZE_LIMIT="65536"       # Maximum response size in bytes; set to 0 to disable
```

### Authentication methods

| Method | Environment variables | Description |
|---|---|---|
| Account and password | `ZSTACK_ACCOUNT` + `ZSTACK_PASSWORD` | Automatically logs in and obtains a session |
| Session ID | `ZSTACK_SESSION_ID` | Uses an existing session and takes precedence over account/password authentication |

> 💡 If both `ZSTACK_SESSION_ID` and account/password credentials are configured, the session ID takes precedence.

### Security

**By default, only read-only APIs are allowed**, including:

- `Query*` — query operations
- `Get*` — get operations
- `List*` — list operations
- `Describe*` — describe operations
- `Check*` — check operations
- `Count*` — count operations
- Other read-only operations

To enable write APIs such as `CreateVmInstance` and `DeleteVolume`, set:

```bash
export ZSTACK_ALLOW_ALL_API="true"
```

> ⚠️ **Warning:** When write operations are enabled, an AI assistant can create, delete, and modify resources. Enable this option with care.

### Query response controls

The server injects `limit=50` into Query APIs by default to prevent a single request from filling the model context window. If a response exceeds 64 KiB, the server truncates the `inventories` list while preserving valid JSON.

| Environment variable | Default | Description |
|---|---:|---|
| `ZSTACK_QUERY_DEFAULT_LIMIT` | `50` | Default limit injected when a Query API does not specify one; set to `0` to disable |
| `ZSTACK_RESPONSE_SIZE_LIMIT` | `65536` | Maximum response size in bytes; oversized responses are truncated; set to `0` to disable |

- An explicitly supplied `limit` is never overwritten.
- A truncated response includes a `_truncation` field suggesting pagination with `limit`/`start` or response reduction with `fields`.

## Usage

### Run as an MCP server

```bash
# Run directly with uvx (no installation required)
uvx zsvirt-mcp-server

# Or use pipx
pipx run zsvirt-mcp-server

# Run the installed command
zsvirt-mcp-server
```

### SSE transport

The default transport is stdio. Use command-line options or environment variables to enable SSE:

```bash
# Command-line options
uvx zsvirt-mcp-server --transport sse --host 0.0.0.0 --port 8000

# Environment variables
export MCP_TRANSPORT="sse"
export MCP_HOST="0.0.0.0"
export MCP_PORT="8000"
export MCP_PATH="/sse"  # Optional
uvx zsvirt-mcp-server
```

> The server also supports the native FastMCP variables `FASTMCP_HOST`, `FASTMCP_PORT`, and `FASTMCP_MOUNT_PATH`.

### Streamable HTTP transport

```bash
# Command-line options
uvx zsvirt-mcp-server --transport streamable-http --host 0.0.0.0 --port 8000 --streamable-path /mcp

# Environment variables
export MCP_TRANSPORT="streamable-http"
export MCP_HOST="0.0.0.0"
export MCP_PORT="8000"
export MCP_STREAMABLE_PATH="/mcp"  # Optional
uvx zsvirt-mcp-server
```

> The server also supports `FASTMCP_STREAMABLE_HTTP_PATH`.

### HTTP header authentication (multi-tenant mode)

In SSE or Streamable HTTP mode, an administrator can run a shared MCP server while each user supplies their own credentials through HTTP headers.

| HTTP header | Environment variable | Description |
|---|---|---|
| `X-ZStack-Account` | `ZSTACK_ACCOUNT` | Account name |
| `X-ZStack-Password` | `ZSTACK_PASSWORD` | Password |
| `X-ZStack-Session-Id` | `ZSTACK_SESSION_ID` | Existing session; takes precedence over account/password authentication |
| `X-ZStack-API-URL` | `ZSTACK_API_URL` | ZStack management node endpoint; allows proxying multiple environments |

Credential precedence: HTTP headers > environment variables.

```bash
# Start a shared MCP server
ZSTACK_ALLOW_ALL_API=false uvx zsvirt-mcp-server --transport streamable-http --host 0.0.0.0 --port 8000
```

Users can configure credentials as HTTP headers in an MCP client:

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

- Sessions are cached and reused for the same account.
- Requests with different `X-ZStack-API-URL` values are routed to different ZStack environments.
- stdio mode has no HTTP headers and automatically falls back to environment-variable authentication.

### Claude Desktop configuration

Add the server to `claude_desktop_config.json`.

**Method 1: account and password**

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

**Method 2: session ID**

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

> 💡 Set `ZSTACK_ALLOW_ALL_API` to `"true"` to enable create, delete, and modify operations.

## Available tools

### 1. `search_api`

Search ZStack APIs by keyword.

**Parameters:**

- `keywords` (`list[str]`): Search keywords, for example `["Query", "Vm"]`
- `category` (`str`, optional): Filter by category
- `limit` (`int`, default: `15`): Maximum number of results

### 2. `describe_api`

Retrieve detailed parameter documentation for an API.

**Parameters:**

- `api_name` (`str`): API name, for example `QueryVmInstance`

### 3. `execute_api`

Invoke a ZStack API.

**Parameters:**

- `api_name` (`str`): API name
- `parameters` (`dict`): API parameters

### 4. `search_metric`

Search available monitoring metrics.

**Parameters:**

- `keywords` (`list[str]`): Search keywords
- `namespace` (`str`, optional): Fuzzy namespace filter, such as `vm` or `host`
- `limit` (`int`, default: `20`): Maximum number of results
- `match_mode` (`str`, default: `or`): Keyword matching mode: `and` or `or`
- `prefer_namespaces` (`list[str]`, optional): Namespaces to rank first; defaults to `["ZStack/VM", "ZStack/Host"]`

> 💡 If the namespace is unknown, omit it first. Search results include namespace values that can be used in a subsequent request.
>
> The default `match_mode` is `or`. Pass `and` explicitly to require all keywords.
>
> Metric names can overlap across namespaces. Specify `namespace` or `prefer_namespaces` to control result ranking.

### 5. `get_metric_data`

Retrieve monitoring data.

**Parameters:**

- `namespace` (`str`): Namespace
- `metric_name` (`str`): Metric name
- `start_time` (`str | int`, optional): Start time as ISO text or a Unix timestamp in seconds
- `end_time` (`str | int`, optional): End time as ISO text or a Unix timestamp in seconds
- `period` (`int`, default: `60`): Sampling period in seconds
- `labels` (`list[str] | dict`, optional): Label filters such as `["VMUuid=xxx"]` or `{"VMUuid": "xxx"}`
- `summary_only` (`bool`, optional): Return only statistics: count, maximum, minimum, average, variance, and standard deviation

**Response size guidance:**

```text
estimated_points = ceil((end_time - start_time) / period) * series_count
```

`series_count` is the number of unique label combinations. Omitting `labels` can return multiple series. Reduce output size by shortening the time range, increasing `period`, or adding label filters.

### 6. `get_metric_summary`

Retrieve aggregated Top-N metric results grouped by a label key.

**Parameters:**

- `namespace` (`str`): Namespace
- `metric_name` (`str`): Metric name
- `label_key` (`str`): Grouping label, such as `VMUuid` or `HostUuid`
- `metric_names` (`list[str]`, optional): Metrics to combine, such as inbound and outbound metrics
- `start_time` (`str | int`, optional): Start time as ISO text or a Unix timestamp in seconds
- `end_time` (`str | int`, optional): End time as ISO text or a Unix timestamp in seconds
- `period` (`int`, default: `60`): Sampling period in seconds
- `aggregate` (`str`, default: `max`): Per-metric aggregation: `max`, `avg`, `sum`, or `min`
- `combine` (`str`, default: `sum`): Multi-metric combination: `sum`, `avg`, `max`, or `min`
- `threshold_op` (`str`, optional): Comparison operator: `>`, `>=`, `<`, `<=`, `==`, or `!=`
- `threshold_value` (`number`, optional): Threshold value
- `top_n` (`int`, default: `10`): Number of results
- `resolve_resource` (`str`, optional): `vm` or `host`, used to resolve resource names

## Query API condition syntax

For Query APIs, the `conditions` parameter supports these operators:

| Operator | Meaning | Example |
|---|---|---|
| `=` | Equal | `name=test` |
| `!=` | Not equal | `state!=Deleted` |
| `>` | Greater than | `cpuNum>4` |
| `>=` | Greater than or equal | `memorySize>=1073741824` |
| `<` | Less than | `createDate<2024-01-01` |
| `<=` | Less than or equal | |
| `?=` | Fuzzy match (`LIKE`; some versions use `like`) | `name?=%test%` |
| `!?=` | Fuzzy non-match | |
| `~=` | Regular-expression match | `name~=.*test.*` |
| `!~=` | Regular-expression non-match | |
| `=null` | Is null | `description=null` |
| `!=null` | Is not null | |
| `in` | In list | `state?=Running,Stopped` |
| `not in` | Not in list | `state!?=Deleted,Destroyed` |

**`conditions` format:**

```json
{
  "conditions": [
    {"name": "uuid", "op": "=", "value": "xxx"},
    {"name": "state", "op": "in", "value": "Running,Stopped"}
  ]
}
```

## Example interaction

User: "Show me the details of the VM whose UUID starts with `ae6e57a0`."

The AI assistant will:

1. Call `search_api(keywords=["Query", "Vm", "Instance"])`
2. Call `describe_api(api_name="QueryVmInstance")`
3. Call `execute_api(api_name="QueryVmInstance", parameters={"conditions": [{"name": "uuid", "op": "?=", "value": "ae6e57a0%"}]})`

## Development

```bash
# Clone the repository
git clone https://github.com/ZSvirt/zsvirt-mcp-server.git
cd zsvirt-mcp-server

# Install development dependencies
pip install -e ".[dev]"

# Run tests
pytest
```

## License

MIT



