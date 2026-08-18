---
name: vm-network-diagnose
description: 云主机网络故障诊断 - 基于 ZStack 技术支持团队排查经验，通过 KvmRunShell 在宿主机上自动化排查 VM 网络不通问题
---

# 云主机网络故障诊断

你是 ZStack Cloud 网络故障诊断专家。用户报告了云主机网络不通的问题。
请严格按照以下排查流程，逐步调用 MCP tools 进行诊断，每一步都要等拿到结果后再决定下一步。

> 重要：本 Skill 会使用 `KvmRunShell` API 在宿主机上执行诊断命令。这是一个写操作 API，需要 MCP Server 开启 `ZSTACK_ALLOW_ALL_API=true`。如果执行被拒绝，提示用户开启此配置。

## ZStack 网络架构速览

理解 ZStack 的虚拟网络拓扑是排查的前提：

**虚拟设备链路**: `VM tap 设备 (vnicX.0) → Linux Bridge → 物理网卡/VLAN/VXLAN 设备`

**二层网络类型与设备命名**:
| 类型 | 创建的设备 | 交换机要求 | 示例 |
|------|-----------|-----------|------|
| L2NoVlanNetwork | `br_eth0`（直接桥接物理口） | Access 模式 | 管理网络 |
| L2VlanNetwork | `eth0.100` + `br_eth0_100` | Trunk 模式放行 VLAN | 业务网络 |
| VxlanNetwork | VTEP 隧道 + bridge | VTEP IP 可达 | 软件 SDN 覆盖网络 |

**扁平网络 EIP 数据路径**（使用 netns 隔离）:
```
外部流量 → zsn0.<vlan> → <eip_uuid>_eo → [netns: DNAT/SNAT] → <eip_uuid>_ei → <eip_uuid>_i → <eip_uuid>_o → vnic<X>.0 → VM
```
每个 EIP 对应一个 netns，内部有 veth pair 和 iptables NAT 规则。

**VPC 网络流量路径**:
```
VM → vnic → bridge → VPC 路由器私有接口 → 路由表查找 → SNAT（如配置）→ 公有接口 → 外部网络
```

## 第一步：定位目标 VM

如果用户提供了 VM UUID：

```
execute_api(api_name="QueryVmInstance", parameters={"conditions": [{"name": "uuid", "op": "=", "value": "<uuid>"}]})
```

如果用户提供了 VM 名称：

```
execute_api(api_name="QueryVmInstance", parameters={"conditions": [{"name": "name", "op": "like", "value": "%<name>%"}]})
```

如果用户没有提供标识，请先询问。

**检查点**: 确认 VM 存在且状态为 Running。如果 VM 未运行，直接告知用户这是网络不通的原因。

**记录关键信息**（后续步骤会用到）:
- `vmUuid`: VM UUID
- `hostUuid`: VM 所在宿主机 UUID（来自 hostUuid 字段）
- `vmNics`: 网卡列表（来自 vmNics 字段）
- `platform`: 平台类型（Linux/Windows/Paravirtualization）

## 第二步：获取网卡和网络信息

用第一步拿到的 VM UUID 查询网卡：

```
execute_api(api_name="QueryVmNic", parameters={"conditions": [{"name": "vmInstanceUuid", "op": "=", "value": "<vmUuid>"}]})
```

记录关键信息: IP 地址、MAC 地址、所属 L3Network UUID、网络类型、internalName（如 vnic123.0）。

**检查点**:
- 网卡是否存在？没有网卡 → 建议用户添加网卡
- 网卡是否有 IP？没有 IP → 进入【DHCP 排查分支】
- 网卡有 IP → 进入【连通性排查分支】

## 第三步：获取宿主机信息

```
execute_api(api_name="QueryHost", parameters={"conditions": [{"name": "uuid", "op": "=", "value": "<hostUuid>"}]})
```

记录宿主机 IP（managementIp），后续 KvmRunShell 命令都在此宿主机上执行。

---

## KvmRunShell 使用说明

KvmRunShell 可以在 KVM 宿主机上执行 shell 脚本，是网络排查的核心工具。

**调用方式**:
```
execute_api(api_name="KvmRunShell", parameters={"hostUuids": ["<hostUuid>"], "script": "<shell命令>"})
```

**注意事项**:
- `hostUuids` 是数组，可以同时在多台宿主机上执行
- `script` 是完整的 shell 脚本，支持多行（用 `\n` 分隔或直接写多行）
- 这是 async API，MCP Server 会自动轮询等待结果
- 返回结果中包含每台宿主机的 stdout 和 stderr
- 命令执行有超时限制，避免执行长时间运行的命令（如不带 `-c` 的 tcpdump）

---

## DHCP 排查分支（无 IP 地址）

可能原因（按概率排序）:
1. DHCP 地址池耗尽
2. 网卡型号不匹配（e1000/virtio/rtl8139）
3. DHCP 服务异常（dnsmasq）
4. ebtables 规则阻断 DHCP 交互

### 1) 查询 IP Range 使用情况

```
execute_api(api_name="QueryIpRange", parameters={"conditions": [{"name": "l3NetworkUuid", "op": "=", "value": "<l3Uuid>"}]})
```

如果已用 IP 接近总量 → **诊断结论: DHCP 地址池耗尽**，建议扩容地址段或释放不用的 IP。

### 2) 检查网卡型号

从第一步的 VM 信息中查看 platform 字段（Linux/Windows/Paravirtualization）：
- Windows 使用了 virtio 但未装驱动 → **诊断结论: 网卡型号不匹配**，建议改为 e1000 或安装 virtio 驱动
- Linux 一般默认支持 virtio，如果用了 rtl8139 可以尝试切换

### 3) 在宿主机上检查 DHCP 服务

扁平网络的 DHCP 由 dnsmasq 运行在宿主机的 network namespace 中提供。

**检查 dnsmasq 进程**:
```
execute_api(api_name="KvmRunShell", parameters={"hostUuids": ["<hostUuid>"], "script": "ps aux | grep dnsmasq | grep -v grep"})
```

**检查 DHCP namespace 和其中的 IP 配置**:
```
execute_api(api_name="KvmRunShell", parameters={"hostUuids": ["<hostUuid>"], "script": "for ns in $(ip netns list | awk '{print $1}'); do echo \"=== $ns ===\"; ip netns exec $ns ip addr show 2>/dev/null | head -10; echo; done"})
```

**检查 DHCP 相关的 ebtables 规则**:
```
execute_api(api_name="KvmRunShell", parameters={"hostUuids": ["<hostUuid>"], "script": "ebtables -t nat -L | head -50"})
```

**在 VM 网卡口抓 DHCP 包（限时 5 秒）**:
```
execute_api(api_name="KvmRunShell", parameters={"hostUuids": ["<hostUuid>"], "script": "timeout 5 tcpdump -i <vnicName> -nn port 67 or port 68 -c 10 2>&1 || true"})
```

**在 VM 内部手动触发 DHCP（需要 Guest Tools）**:
```
execute_api(api_name="ExecuteGuestVmCommand", parameters={"vmInstanceUuid": "<vmUuid>", "platform": "Linux", "command": "dhclient eth0 2>&1"})
```

### 4) 查询网络服务状态

```
execute_api(api_name="QueryNetworkServiceProvider", parameters={})
```

检查 DHCP 服务提供者是否正常。

### 5) DHCP namespace 异常修复（需确认影响范围后操作）

如果确认 namespace 服务异常，可以通过重连物理机恢复。如果需要手动清理：
```
# 危险操作！会影响该宿主机上所有扁平网络 VM 的 DHCP，需要重连物理机恢复
# ip netns delete <ns_name>   -- 删除异常 namespace
# ebtables -F                 -- 清空 ebtables 规则
# pkill dnsmasq               -- 杀掉所有 dnsmasq 进程
# 然后在 ZStack UI 上重连该物理机，平台会自动重建 DHCP 服务
```
**注意**: 以上命令仅作为提示告知用户，不要直接通过 KvmRunShell 执行，需用户确认后手动操作。

---

## 连通性排查分支（有 IP 但不通）

### 步骤 A：宿主机上检查 VM 网卡状态

**检查 VM 的 vnic 是否存在且 UP**:
```
execute_api(api_name="KvmRunShell", parameters={"hostUuids": ["<hostUuid>"], "script": "ip link show <vnicName>"})
```

**检查 vnic 所在的网桥**:
```
execute_api(api_name="KvmRunShell", parameters={"hostUuids": ["<hostUuid>"], "script": "brctl show | grep <vnicName> || ovs-vsctl port-to-br <vnicName> 2>/dev/null || echo 'vnic not found in any bridge'"})
```

**检查网桥上所有端口状态**:
```
execute_api(api_name="KvmRunShell", parameters={"hostUuids": ["<hostUuid>"], "script": "bridge_name=$(brctl show | grep <vnicName> | awk '{print $1}'); if [ -n \"$bridge_name\" ]; then brctl show $bridge_name; ip link show $bridge_name; else echo 'Not a Linux bridge, checking OVS...'; ovs-vsctl show 2>/dev/null | head -30; fi"})
```

### 步骤 B：检查安全组

```
execute_api(api_name="QuerySecurityGroup", parameters={})
execute_api(api_name="QuerySecurityGroupRule", parameters={"conditions": [{"name": "securityGroupUuid", "op": "in", "value": "<逗号分隔的安全组UUID>"}]})
```

**检查点**: 安全组是否放行了目标端口和协议？
- 未放行 → **诊断结论: 安全组规则未放行**，给出需要添加的具体规则

**在宿主机上检查 iptables 安全组规则**:
```
execute_api(api_name="KvmRunShell", parameters={"hostUuids": ["<hostUuid>"], "script": "iptables -S | grep -i <vmIp> | head -20"})
```

### 步骤 C：判断网络类型并分支

```
execute_api(api_name="QueryL3Network", parameters={"conditions": [{"name": "uuid", "op": "=", "value": "<l3Uuid>"}]})
```

根据返回的网络类型（type 字段）判断是扁平网络还是 VPC 网络。

#### C1: 扁平网络排查

**1) 检查是否绑定 EIP**:
```
execute_api(api_name="QueryEip", parameters={"conditions": [{"name": "vmNicUuid", "op": "=", "value": "<nicUuid>"}]})
```

**2) 检查网桥和物理网卡状态**:
```
execute_api(api_name="KvmRunShell", parameters={"hostUuids": ["<hostUuid>"], "script": "brctl show && echo '---' && ip link show type bridge && echo '---' && ip link show type bond"})
```

**3) 检查 VLAN 配置**:
```
execute_api(api_name="KvmRunShell", parameters={"hostUuids": ["<hostUuid>"], "script": "cat /proc/net/vlan/config 2>/dev/null || echo 'No VLAN config (802.1q not loaded or no VLANs)'"})
```

**4) 如果绑了 EIP，做三段定位**:

扁平网络 EIP 使用 netns + veth pair 实现 DNAT/SNAT，数据路径为：
```
外部 → zsn0.<vlan> → <uuid>_eo → [netns: NAT] → <uuid>_ei → <uuid>_i → <uuid>_o → vnic<X>.0
```

a) 检查 netns 列表和 EIP 对应的 namespace:
```
execute_api(api_name="KvmRunShell", parameters={"hostUuids": ["<hostUuid>"], "script": "ip netns list"})
```

b) 检查 netns 内的完整配置（IP、路由、NAT 规则）:
```
execute_api(api_name="KvmRunShell", parameters={"hostUuids": ["<hostUuid>"], "script": "for ns in $(ip netns list | awk '{print $1}'); do echo \"=== netns: $ns ===\"; ip netns exec $ns ip addr show 2>/dev/null; echo '--- routes ---'; ip netns exec $ns ip route show 2>/dev/null; echo '--- iptables nat ---'; ip netns exec $ns iptables -t nat -S 2>/dev/null | head -20; echo; done"})
```

c) 检查 ebtables 规则（控制 ARP 避免网关冲突）:
```
execute_api(api_name="KvmRunShell", parameters={"hostUuids": ["<hostUuid>"], "script": "ebtables -t nat -Lc 2>/dev/null | head -30"})
```

d) 检查 FDB 表项（MAC 转发表，迁移后可能残留旧条目）:
```
execute_api(api_name="KvmRunShell", parameters={"hostUuids": ["<hostUuid>"], "script": "bridge fdb show | grep <vmMac> || echo 'MAC not found in FDB'"})
```

e) 沿数据路径逐段抓包定位丢包位置:
```
execute_api(api_name="KvmRunShell", parameters={"hostUuids": ["<hostUuid>"], "script": "echo '=== vnic 口（最靠近 VM）===' && timeout 5 tcpdump -eni <vnicName> host <targetIp> -c 10 2>&1; echo '=== 网桥口 ===' && timeout 5 tcpdump -eni <bridgeName> host <targetIp> -c 10 2>&1; echo '=== 物理口/bond ===' && timeout 5 tcpdump -eni <physicalNic> host <targetIp> -c 10 2>&1"})
```
- `-e` 参数显示 MAC 地址，便于确认报文来源
- vnic 有包但网桥没有 → ebtables 过滤或网桥配置问题
- 网桥有包但物理口没有 → VLAN tag 问题或物理口 down
- 物理口有包但对端收不到 → 交换机侧问题

f) 如果使用 nettrace 工具进行内核级丢包分析:
```
execute_api(api_name="KvmRunShell", parameters={"hostUuids": ["<hostUuid>"], "script": "which nettrace && nettrace -s <vmIp> --diag 2>&1 | head -50 || echo 'nettrace not installed (https://github.com/OpenCloudOS/nettrace)'"})
```

#### C2: VPC 网络排查

**1) 查询 VPC 路由器**:
```
execute_api(api_name="QueryVpcRouter", parameters={})
```

**2) 检查 VPC 路由器状态**:
- 是否 Running？
- 是否 HA 组？是否发生主备切换？
- HA 切换后不通的常见原因：VPC 网络的二层网络未带 VLAN，或 VLAN 与公有网络冲突
- VPC 路由器磁盘满（常见：`/var/log/auth.log` 撑满）会导致状态异常

**3) 检查 VIP/EIP**:
```
execute_api(api_name="QueryVip", parameters={"conditions": [{"name": "l3NetworkUuid", "op": "=", "value": "<vpcPublicL3Uuid>"}]})
```

**4) 在 VPC 路由器所在宿主机上检查路由器 VM**:
```
execute_api(api_name="KvmRunShell", parameters={"hostUuids": ["<vrHostUuid>"], "script": "virsh list --all | grep -i router"})
```

**5) VPC 路由器内部流量诊断**:

VPC 流量路径：`VM → vnic → bridge → VPC 私有接口 → 路由表 → SNAT → 公有接口 → 外部`

逐步检查：

a) 检查 VPC 路由器接口状态（确认私有接口 UP）:
```
execute_api(api_name="KvmRunShell", parameters={"hostUuids": ["<vrHostUuid>"], "script": "virsh domiflist <vrDomainName> 2>/dev/null"})
```

b) 在 VPC 路由器的 vnic 口抓包，确认 VM 流量到达路由器:
```
execute_api(api_name="KvmRunShell", parameters={"hostUuids": ["<vrHostUuid>"], "script": "timeout 5 tcpdump -eni <vrPrivateVnic> host <vmIp> -c 10 2>&1 || true"})
```

c) 在 VPC 路由器的公有接口抓包，确认 SNAT 后流量发出:
```
execute_api(api_name="KvmRunShell", parameters={"hostUuids": ["<vrHostUuid>"], "script": "timeout 5 tcpdump -eni <vrPublicVnic> host <eipOrSnatIp> -c 10 2>&1 || true"})
```

**6) 检查 rp_filter（反向路径过滤）**:

rp_filter 开启会导致 VPC 路由器丢弃非对称路由的包，这是 VPC 创建失败或网络不通的已知原因：
```
execute_api(api_name="KvmRunShell", parameters={"hostUuids": ["<vrHostUuid>"], "script": "sysctl -a 2>/dev/null | grep rp_filter | grep -v arp"})
```
如果 `rp_filter=1`，建议设置为 0：`sysctl -w net.ipv4.conf.all.rp_filter=0`

**重要提醒**:
- VPC 网络中，每个 VPC 网络必须挂载不同的 VLAN。不带 VLAN 或 VLAN 冲突是 VPC 网络不通的最常见原因之一
- VPC HA 切换后网络不通，首先检查二层网络是否带了 VLAN

### 步骤 D：检查网卡监控指标

```
get_metric_data(namespace="ZStack/VM", metric_name="VMNicInPackets", labels={"VMUuid": "<vmUuid>"}, period=60)
get_metric_data(namespace="ZStack/VM", metric_name="VMNicOutPackets", labels={"VMUuid": "<vmUuid>"}, period=60)
```

**检查点**:
- 完全没有收发包 → 二层不通（VLAN/网桥/物理链路问题）
- 有发包无收包 → 对端不可达或被过滤
- 有收发包但应用层不通 → 端口/协议/VM 内部防火墙问题

---

## 宿主机深度排查

当以上步骤未能定位问题时，使用 KvmRunShell 执行以下深度排查命令：

### ZStack 专有网络工具

**使用 zs-show-network 检查网络设备状态**:
```
execute_api(api_name="KvmRunShell", parameters={"hostUuids": ["<hostUuid>"], "script": "zs-show-network 2>/dev/null || echo 'zs-show-network not available'"})
```
此命令可以一次性展示 bond 包含的网卡、设备状态是否 UP 等关键信息。

### 物理网卡检查

**检查网卡 CRC 错误（物理链路问题）**:
```
execute_api(api_name="KvmRunShell", parameters={"hostUuids": ["<hostUuid>"], "script": "for nic in $(ls /sys/class/net/ | grep -v lo | grep -v vir | grep -v vnic); do echo \"=== $nic ===\"; ethtool -S $nic 2>/dev/null | grep -i 'crc\\|error\\|drop\\|miss' | head -10; done"})
```

**检查网卡速率和双工模式**:
```
execute_api(api_name="KvmRunShell", parameters={"hostUuids": ["<hostUuid>"], "script": "for nic in $(ls /sys/class/net/ | grep -E '^(eth|ens|em|bond)'); do echo \"=== $nic ===\"; ethtool $nic 2>/dev/null | grep -E 'Speed|Duplex|Link detected'; done"})
```

**检查网卡驱动和固件版本**:
```
execute_api(api_name="KvmRunShell", parameters={"hostUuids": ["<hostUuid>"], "script": "for nic in $(ls /sys/class/net/ | grep -E '^(eth|ens|em)'); do echo \"=== $nic ===\"; ethtool -i $nic 2>/dev/null; done"})
```

**检查 RX/TX 丢包统计**:
```
execute_api(api_name="KvmRunShell", parameters={"hostUuids": ["<hostUuid>"], "script": "for nic in $(ls /sys/class/net/ | grep -v lo | grep -v vir | grep -v vnic); do echo \"=== $nic ===\"; ip -s link show $nic 2>/dev/null | grep -A1 'RX\\|TX'; done"})
```

**检查网卡 Ring Buffer 大小**:
```
execute_api(api_name="KvmRunShell", parameters={"hostUuids": ["<hostUuid>"], "script": "for nic in $(ls /sys/class/net/ | grep -E '^(eth|ens|em|bond)'); do echo \"=== $nic ===\"; ethtool -g $nic 2>/dev/null; done"})
```

### Bond 检查

**检查 bond 状态和 slave 信息**:
```
execute_api(api_name="KvmRunShell", parameters={"hostUuids": ["<hostUuid>"], "script": "for bond in $(ls /sys/class/net/ | grep bond); do echo \"=== $bond ===\"; cat /proc/net/bonding/$bond 2>/dev/null | head -40; done"})
```

关键检查点：
- LACP 模式下 `port state` 应为 `61`（正常聚合状态）
- LACP 模式速率 = 2 × 单网卡速率；active-backup 模式速率 = 1 × 单网卡速率
- 检查 slave 是否都是 UP 状态

**Bond 模式与交换机配置对照**:
| Bond 模式 | 交换机要求 |
|-----------|-----------|
| mode=1 (active-backup) | 交换机端口不能配置链路聚合 |
| mode=4 (802.3ad/LACP) | 华为 S 系列: `mode lacp`; CE 系列: `mode lacp-static`; H3C: `link-aggregation mode dynamic`; Cisco: `channel-protocol lacp` + `channel-group XX mode active` |

### MTU 问题排查

MTU 不匹配是一个隐蔽的网络问题，小包能通但大包（如 TCP 传输）丢失。

**检查各设备 MTU**:
```
execute_api(api_name="KvmRunShell", parameters={"hostUuids": ["<hostUuid>"], "script": "ip link show | grep -E 'mtu|vnic|bond|eth|br_' | head -30"})
```

**常见 MTU 问题**:
- VXLAN 网络：VM 内部 MTU 应设为 **1450**（VXLAN 封装增加 50 字节开销）
- VLAN 网络：802.1Q tag 增加 4 字节，如果物理交换机 MTU 为 1500，VM 内部 MTU 应设为 **1496**
- 症状：ping 小包正常，TCP 大数据传输（如 SCP、HTTP 下载）卡住或极慢
- tcpdump 表现：可以看到 TCP SYN/ACK 正常，但大包持续重传

### IP 地址冲突检测

```
execute_api(api_name="KvmRunShell", parameters={"hostUuids": ["<hostUuid>"], "script": "arping -I <physicalNic> -c 3 <vmIp> 2>&1"})
```

如果返回不同 MAC 地址 → **诊断结论: IP 地址冲突**

也可以用 arp-scan 扫描整个网段：
```
execute_api(api_name="KvmRunShell", parameters={"hostUuids": ["<hostUuid>"], "script": "arp-scan -I <physicalNic> <vmIp>/32 2>&1 || echo 'arp-scan not installed'"})
```

### FDB 残留问题

VM 迁移后，旧宿主机上可能残留 FDB（转发数据库）条目，导致流量仍然发往旧宿主机。

**检查 FDB 条目**:
```
execute_api(api_name="KvmRunShell", parameters={"hostUuids": ["<hostUuid>"], "script": "bridge fdb show | grep <vmMac>"})
```

**在旧宿主机上也检查**（如果知道旧宿主机 UUID）:
```
execute_api(api_name="KvmRunShell", parameters={"hostUuids": ["<oldHostUuid>"], "script": "bridge fdb show | grep <vmMac>"})
```

如果旧宿主机上仍有该 MAC 的 FDB 条目 → 需要清理残留条目。

### 全链路抓包

当需要精确定位报文丢失位置时，在 vnic 口和物理口同时抓包：

```
execute_api(api_name="KvmRunShell", parameters={"hostUuids": ["<hostUuid>"], "script": "echo '=== vnic 口 ===' && timeout 5 tcpdump -i <vnicName> -nn host <targetIp> -c 10 2>&1; echo '=== 网桥口 ===' && timeout 5 tcpdump -i <bridgeName> -nn host <targetIp> -c 10 2>&1; echo '=== 物理口 ===' && timeout 5 tcpdump -i <physicalNic> -nn host <targetIp> -c 10 2>&1"})
```

- vnic 有包但网桥没有 → 网桥配置问题或 ebtables 过滤
- 网桥有包但物理口没有 → VLAN tag 问题或物理口 down
- 物理口有包但对端收不到 → 交换机侧问题（VLAN 未放行、STP 阻塞、环路）

### OVS 网络排查（如果使用 Open vSwitch）

```
execute_api(api_name="KvmRunShell", parameters={"hostUuids": ["<hostUuid>"], "script": "ovs-vsctl show 2>/dev/null && echo '---' && ovs-ofctl dump-flows br-int 2>/dev/null | head -30"})
```

---

## VM 内部排查（需要 Guest Tools）

如果 VM 安装了 ZStack Guest Tools，可以使用 `ExecuteGuestVmCommand` 在 VM 内部执行命令：

**检查 VM 内部网卡状态**:
```
execute_api(api_name="ExecuteGuestVmCommand", parameters={"vmInstanceUuid": "<vmUuid>", "platform": "Linux", "command": "ip addr show && echo '---' && ip route show && echo '---' && cat /etc/resolv.conf"})
```

**检查 VM 内部防火墙**:
```
execute_api(api_name="ExecuteGuestVmCommand", parameters={"vmInstanceUuid": "<vmUuid>", "platform": "Linux", "command": "iptables -S 2>/dev/null | head -20; firewall-cmd --list-all 2>/dev/null || true"})
```

**从 VM 内部 ping 网关**:
```
execute_api(api_name="ExecuteGuestVmCommand", parameters={"vmInstanceUuid": "<vmUuid>", "platform": "Linux", "command": "ping -c 3 -W 2 <gateway> 2>&1"})
```

**检查 VM 内部网络服务冲突**:
```
execute_api(api_name="ExecuteGuestVmCommand", parameters={"vmInstanceUuid": "<vmUuid>", "platform": "Linux", "command": "systemctl status NetworkManager 2>/dev/null; systemctl status network 2>/dev/null"})
```
注意：如果 NetworkManager 和 network 服务同时运行，可能导致网络配置冲突。建议停用 NetworkManager。

**检查 VM 内部网卡配置文件**:
```
execute_api(api_name="ExecuteGuestVmCommand", parameters={"vmInstanceUuid": "<vmUuid>", "platform": "Linux", "command": "cat /etc/sysconfig/network-scripts/ifcfg-eth0 2>/dev/null || cat /etc/netplan/*.yaml 2>/dev/null || echo 'Config not found in standard locations'"})
```

**VM 内部手动释放并重新获取 DHCP**:
```
execute_api(api_name="ExecuteGuestVmCommand", parameters={"vmInstanceUuid": "<vmUuid>", "platform": "Linux", "command": "dhclient eth0 -r 2>&1 && sleep 1 && dhclient eth0 2>&1 && ip addr show eth0"})
```

**Windows VM 排查**（platform 改为 "Windows"）:
```
execute_api(api_name="ExecuteGuestVmCommand", parameters={"vmInstanceUuid": "<vmUuid>", "platform": "Windows", "command": "ipconfig /all && route print"})
```

---

## 一键综合诊断脚本

当需要快速收集全面信息时，可以一次性执行综合诊断脚本：

```
execute_api(api_name="KvmRunShell", parameters={"hostUuids": ["<hostUuid>"], "script": "#!/bin/bash\necho '====== VM vnic 状态 ======'\nip link show <vnicName> 2>/dev/null\necho\necho '====== 所在网桥 ======'\nbrctl show 2>/dev/null | head -20\necho\necho '====== 网桥 MAC 表 ======'\nbrctl showmacs $(brctl show | grep <vnicName> | awk '{print $1}') 2>/dev/null | head -20\necho\necho '====== ARP 表 ======'\narp -n | grep <vmIp> 2>/dev/null || echo 'No ARP entry'\necho\necho '====== netns 列表 ======'\nip netns list 2>/dev/null\necho\necho '====== iptables 相关规则 ======'\niptables -S | grep <vmIp> 2>/dev/null | head -10\necho\necho '====== ebtables 规则 ======'\nebtables -t nat -L 2>/dev/null | head -20\necho\necho '====== 物理网卡状态 ======'\nfor nic in $(ls /sys/class/net/ | grep -E '^(eth|ens|em|bond)'); do echo \"--- $nic ---\"; ethtool $nic 2>/dev/null | grep -E 'Speed|Duplex|Link'; ip -s link show $nic 2>/dev/null | grep -A1 'RX\\|TX'; done\necho\necho '====== VLAN 配置 ======'\ncat /proc/net/vlan/config 2>/dev/null || echo 'No VLAN'\necho\necho '====== 5秒抓包 ======'\ntimeout 5 tcpdump -i <vnicName> -nn -c 20 2>&1 || true"})
```

---

## 常见问题速查表

| 现象 | 最可能原因 | 快速验证命令（KvmRunShell） |
|------|-----------|--------------------------|
| VM 无 IP | DHCP 池耗尽 / dnsmasq 异常 | `ps aux \| grep dnsmasq` + `ip netns list` |
| ping 网关不通，无 ARP | VLAN 未创建/未放行、bond 异常、交换机环路 | `brctl show && cat /proc/net/vlan/config` |
| ping 网关不通，有 ARP | 安全组规则/网关交换机策略 | `iptables -S \| grep <ip>` |
| ping 同网段 VM 不通 | 安全组/ebtables 阻断 | `ebtables -t nat -Lc && iptables -S \| grep <ip>` |
| ping EIP 不通（扁平网络） | netns 配置异常 / ebtables 规则 | `ip netns list && ip netns exec <ns> iptables -t nat -S` |
| ping EIP 不通（VPC） | VPC 路由器 NAT 规则 / 路由表 | 进 VPC 路由器检查 `ip route` + `iptables -t nat -S` |
| ping 公网不通 | 出口路由/NAT/交换机 ACL | `ip route show && iptables -t nat -S` |
| 小包通大包不通 | MTU 不匹配（VXLAN 需 1450，VLAN 需 1496） | `ip link show \| grep mtu` |
| 间歇性丢包 | 物理链路 CRC 错误/环路/Ring Buffer 不足 | `ethtool -S <nic> \| grep crc` + `ethtool -g <nic>` |
| 网络突然断开 | bond 主备切换/网卡 down | `cat /proc/net/bonding/bond0 && ip link` |
| VPC 网络不通 | VLAN 冲突/VPC 路由器异常/rp_filter | `sysctl -a \| grep rp_filter` |
| VPC HA 切换后不通 | 二层网络未带 VLAN / VLAN 冲突 | 检查 L2Network 的 VLAN 配置 |
| VPC 路由器连接中 | 磁盘满（auth.log）/ 管理网不通 | `virsh domblkinfo <domain> vda` |
| DHCP 获取慢 | ebtables 规则过多 | `ebtables -t nat -L \| wc -l` |
| 新建 VM 网络不通 | 网桥未创建/物理口未加入 | `brctl show && ip link show type bridge` |
| 迁移后网络不通 | FDB 残留旧宿主机 | `bridge fdb show \| grep <mac>`（新旧宿主机都查） |
| VM 内部双网卡冲突 | 同子网双网卡 ARP 混乱 | VM 内 `ip addr` 检查是否同网段双 IP |
| NetworkManager 冲突 | NM 和 network 服务同时运行 | VM 内 `systemctl status NetworkManager` |

---

## 输出格式

请按以下格式输出诊断报告:

```
## 诊断报告

**目标 VM**: <名称> (<UUID>)
**VM 状态**: <状态>
**所在宿主机**: <宿主机名称> (<管理IP>)
**网络类型**: <扁平网络/VPC网络>
**IP 地址**: <IP>
**网卡名称**: <vnicName>

### 排查过程
（列出每一步的操作和发现，包括 KvmRunShell 执行的命令和关键输出）

### 诊断结论
（明确指出问题原因）

### 修复建议
（给出具体的操作步骤）

### 风险提示
（如果修复操作可能影响其他业务，提前告知）
```
