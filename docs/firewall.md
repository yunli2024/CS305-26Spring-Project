# Firewall Development Guide

本文档总结 README 中与本人负责的 firewall 部分相关的要求，并给出后续实现的逐步执行步骤。

## Goal

Firewall 的目标是利用 SDN 集中式控制能力，在 controller 中解析防火墙规则，并把规则安装成交换机流表。交换机在数据平面直接 drop 匹配规则的包，不需要每个 host 单独过滤。

本项目 firewall 主要支持 `deny` 规则。

## Rule Format

README 要求防火墙规则由 JSON 文件定义。每条规则可能包含：

- `src_ip`: 源 IP 地址。
- `dst_ip`: 目的 IP 地址。
- `proto`: 传输层协议，例如 `icmp`、`tcp`、`udp`。
- `src_port`: 源端口。
- `dst_port`: 目的端口。
- `action`: 规则动作，本项目主要处理 `deny`。

通配字段：

- 空值
- `*`
- `any`

这些都应被视为 wildcard，不参与对应字段匹配。

注意：README 中写的是 `firewall_rules.json`，当前仓库实际存在的是 `firewall_rule.json`，而 `firewall.py` 当前默认参数是 `firewall_rules.json`。实现前需要统一这个文件名，否则规则可能无法被读取。

当前 `firewall_rule.json` 中规则为：

```json
{
  "rules": [
    {
      "src_ip": "192.168.117.2",
      "dst_ip": "192.168.117.3",
      "proto": "icmp",
      "src_port": "*",
      "dst_port": "*",
      "action": "deny"
    },
    {
      "src_ip": "192.168.117.2",
      "dst_ip": "192.168.117.3",
      "proto": "tcp",
      "src_port": "*",
      "dst_port": 80,
      "action": "deny"
    }
  ]
}
```

## Expected Behavior

测试拓扑为一个 switch 连接三个 hosts：

```text
h1 ---\
h2 ---- s1
h3 ---/
```

测试脚本手动配置 IP：

- h1: `192.168.117.2/24`
- h2: `192.168.117.3/24`
- h3: `192.168.117.4/24`

README 中的测试期望：

- h1 -> h2 ICMP: should fail，因为规则阻止 h1 到 h2 的 ICMP。
- h1 -> h3 ICMP: should pass，因为没有规则阻止 h1 到 h3。
- h1 -> h2 TCP/80: should fail，因为规则阻止 h1 到 h2 的 TCP 80。
- h1 -> h2 TCP/8080: should pass，因为规则没有阻止 8080。

## Implementation Idea

Firewall 模块应在 controller 启动或 switch 加入时加载规则，并为每个 switch 安装高优先级 drop flow。

drop flow 的核心特征：

- `dl_type=ether.ETH_TYPE_IP`
- 根据规则设置 `nw_src`
- 根据规则设置 `nw_dst`
- 根据规则设置 `nw_proto`
- 对 TCP/UDP 可设置 `tp_src` 和 `tp_dst`
- `actions=[]`
- priority 高于 shortest path forwarding flow

普通 forwarding flow 当前优先级约为 100，因此 firewall deny flow 可使用 `Firewall.PRIORITY = 60000`。

## Step-by-Step Plan

### Step 1: Unify Rule File Name

先处理规则文件名不一致问题：

- 方案 A：把 `Firewall.__init__` 默认参数改为 `firewall_rule.json`，匹配当前仓库文件。
- 方案 B：在 `controller.py` 中写 `self.firewall = Firewall("firewall_rule.json")`。
- 方案 C：新增一个 `firewall_rules.json` 文件，与 README 保持一致。

推荐方案 A 或 B，因为当前仓库已经有 `firewall_rule.json`。

### Step 2: Implement `_load_rules()`

在 `firewall.py` 中实现：

1. 判断规则文件是否存在。
2. 打开 JSON 文件并解析。
3. 读取顶层 `rules` 数组。
4. 对每个 rule dict 创建 `FirewallRule`。
5. 对缺失字段使用默认值。
6. 返回 `FirewallRule` 列表。

需要支持的字段：

```python
FirewallRule(
    src_ip=item.get("src_ip"),
    dst_ip=item.get("dst_ip"),
    proto=item.get("proto"),
    src_port=item.get("src_port"),
    dst_port=item.get("dst_port"),
    action=item.get("action", "deny"),
)
```

### Step 3: Normalize Rule Fields

使用 `firewall.py` 中已有 helper：

- `_normalize_any()`
- `_normalize_proto()`
- `_proto_to_number()`
- `_normalize_port()`

目标：

- `src_ip`、`dst_ip` 为 wildcard 时传 `0`。
- `proto` 为 wildcard 时传 `0`。
- `src_port`、`dst_port` 为 wildcard 时传 `0`。
- `icmp/tcp/udp` 转成 `inet.IPPROTO_ICMP`、`inet.IPPROTO_TCP`、`inet.IPPROTO_UDP`。

### Step 4: Validate Port Rules

端口只适用于 TCP/UDP：

- 如果 `proto` 是 TCP 或 UDP，可以使用 `src_port` 和 `dst_port`。
- 如果 `proto` 是 ICMP，端口应被忽略或规则中端口必须是 wildcard。
- 如果端口不是数字且不是 wildcard，应跳过该规则或记录错误。

为了通过当前测试，至少要正确支持：

- ICMP wildcard ports。
- TCP dst port 80。
- TCP wildcard src port。

### Step 5: Install Drop Flows

在 `install_rules(self, ofctls)` 中：

1. 遍历 `ofctls.items()`。
2. 遍历 `self.rules`。
3. 只处理 `rule.action.lower() == "deny"`。
4. 计算 normalized match 字段。
5. 构造去重 key，例如：

```python
key = (dpid, src_ip, dst_ip, proto_num, src_port, dst_port, action)
```

6. 如果 key 已在 `self.installed` 中，跳过。
7. 调用 `ofctl.set_flow()`：

```python
ofctl.set_flow(
    cookie=self.COOKIE,
    priority=self.PRIORITY,
    dl_type=ether.ETH_TYPE_IP,
    nw_src=src_ip or 0,
    nw_dst=dst_ip or 0,
    nw_proto=proto_num,
    tp_src=src_port,
    tp_dst=dst_port,
    actions=[],
)
```

注意：当前 `ControllerApp.OFP_VERSIONS = [ofproto_v1_0.OFP_VERSION]`，`OfCtl_v1_0.set_flow()` 支持 `tp_src` 和 `tp_dst`。

### Step 6: Call Firewall Installation From Controller

在 `controller.py` 中找到 switch 加入逻辑：

```python
def handle_switch_add(self, ev):
    datapath = ev.switch.dp
    self.datapaths[datapath.id] = datapath
    self.ofctls[datapath.id] = OfCtl.factory(datapath, self.logger)
    self._install_controller_flows(datapath)
    self._refresh_forwarding_rules()
```

建议在 switch 的 `ofctl` 创建后安装 firewall：

```python
self.firewall.install_rules(self.ofctls)
```

可放在 `_install_controller_flows(datapath)` 后，或 `_refresh_forwarding_rules()` 后。因为 firewall priority 更高，位置不应影响最终匹配优先级，但放在 switch 加入时最直观。

### Step 7: Verify Flow Priority

确保 firewall drop flow priority 高于普通 forwarding flow。

当前 forwarding flow 安装大致为：

```python
ofctl.set_flow(cookie=0, priority=100, dl_dst=dst_mac, ...)
```

Firewall 使用：

```python
PRIORITY = 60000
```

这样 deny flow 会先匹配。

### Step 8: Run Firewall Test

终端 1，在项目根目录启动 controller：

```bash
osken-manager --observe-links controller.py
```

终端 2，运行 firewall 测试：

```bash
cd ./tests/firewall_test/
sudo env "PATH=$PATH" python test_network.py
```

如果 Mininet 环境残留旧拓扑，先清理：

```bash
sudo mn -c
```

### Step 9: Inspect Flow Table If Needed

在 Mininet CLI 中执行：

```bash
dpctl dump-flows
```

应能看到高优先级 IP flow，匹配：

- `192.168.117.2 -> 192.168.117.3` ICMP
- `192.168.117.2 -> 192.168.117.3` TCP dst port 80

drop flow 的 action 应为空。

### Step 10: Expected Test Results

运行测试后检查：

- `h1 ping 192.168.117.3` 应失败。
- `h1 ping 192.168.117.4` 应成功。
- `curl http://192.168.117.3:80/` 应失败或 timeout。
- `curl http://192.168.117.3:8080/` 应成功返回 HTTP code。

如果结果不符合预期，优先检查：

- 规则文件是否被正确读取。
- `install_rules()` 是否真的被调用。
- drop flow 是否安装到所有 switch。
- drop flow priority 是否高于 forwarding flow。
- `nw_proto` 是否正确设置。
- TCP 80 是否设置到了 `tp_dst`，不是 `tp_src`。

