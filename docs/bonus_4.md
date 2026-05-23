# Bonus 4：DNS / NAT 设计、实现与验证说明

本文档对应分支：`bonus_4_DNS_NAT`。

目标是完成 README 中 Bonus 4 的要求：

```text
Implement more functions using os-ken, such as DNS, and NAT.
```

当前分支的实际进度是：

- DNS responder 已经开始实现，并已接入 controller。
- NAT 仍处于设计阶段，尚未实现代码。
- 下一步应优先在 Linux + Mininet + os-ken 环境中验证 DNS。

建议不要同时推进 DNS 和 NAT。先把 DNS 做到可演示、可截图、可写进报告，再决定是否继续实现 NAT。

---

## 1. 整体设计思路

### 1.1 为什么先做 DNS

DNS 是 Bonus 4 中最适合当前项目架构的扩展功能。

原因：

- 当前项目已经有 DHCP、ARP、PacketIn、PacketOut 和 shortest path switching。
- DNS 可以作为新的控制平面服务，由 controller 直接回答 host 的域名查询。
- DNS 不需要改写普通数据流路径，对已有 switching 和 firewall 影响较小。
- 验证方式明确：host 查询域名，controller 返回指定 IP。

相比之下，NAT 需要改写 IP、MAC、端口，并维护连接状态，且 OpenFlow 1.0 的 action 支持需要额外确认。因此 NAT 更适合作为第二阶段。

### 1.2 DNS 功能目标

实现一个 controller-hosted DNS responder：

```text
host -> switch -> controller -> switch -> host
```

具体行为：

1. host 向虚拟 DNS server `192.168.1.1` 发送 UDP/53 DNS 查询。
2. switch 根据高优先级 flow，把 UDP/53 请求送到 controller。
3. controller 从 `dns_records.json` 查找域名。
4. 如果记录存在，controller 构造 DNS A record response。
5. controller 使用 PacketOut 把 DNS response 发回请求 host。
6. 普通 ping、shortest path switching、firewall 不应被 DNS 功能破坏。

默认 DNS 记录：

| 域名 | 解析结果 |
| --- | --- |
| `h1.cs305.local` | `192.168.1.2` |
| `h2.cs305.local` | `192.168.1.3` |
| `web.cs305.local` | `192.168.1.3` |

### 1.3 DNS 与现有模块的关系

DNS 功能和现有模块的关系如下：

```text
DHCP:
  下发 DNS server 地址 192.168.1.1

ARP:
  host 查询 192.168.1.1 的 MAC 时，controller 返回虚拟 DNS MAC

OpenFlow:
  switch 将 UDP/53 packet 送到 controller

DNS module:
  解析 DNS request，构造 DNS response

PacketOut:
  controller 把 DNS response 发回 host

Switching:
  继续负责普通主机之间的最短路径转发

Firewall:
  继续负责高优先级 deny flow，不影响 DNS 逻辑
```

---

## 2. 当前代码实现方式

### 2.1 新增文件：`dns_records.json`

文件作用：保存静态 DNS 解析表。

当前内容：

```json
{
  "server_ip": "192.168.1.1",
  "server_mac": "7e:49:b3:f0:f9:99",
  "ttl": 60,
  "records": {
    "h1.cs305.local": "192.168.1.2",
    "h2.cs305.local": "192.168.1.3",
    "web.cs305.local": "192.168.1.3"
  }
}
```

字段说明：

- `server_ip`：controller 托管的虚拟 DNS server IP。
- `server_mac`：controller 用于 ARP 和 DNS response 的虚拟 MAC。
- `ttl`：DNS answer 的 TTL。
- `records`：静态 A 记录表。

当前只支持 A record，也就是 IPv4 域名解析。

### 2.2 新增文件：`dns_server.py`

文件作用：实现 DNS responder 的核心逻辑。

主要类：

```python
class DNSServer:
    DEFAULT_SERVER_IP = "192.168.1.1"
    DEFAULT_SERVER_MAC = "7e:49:b3:f0:f9:99"
    DEFAULT_TTL = 60
    COOKIE = 0x305D
    PACKETIN_PRIORITY = 2000
    DNS_PORT = 53
```

主要方法：

| 方法 | 作用 |
| --- | --- |
| `_load_records()` | 读取 `dns_records.json` |
| `is_dns_ip()` | 判断某个 IP 是否是虚拟 DNS server IP |
| `install_packetin_flow()` | 给 switch 安装 UDP/53 到 controller 的 flow |
| `handle_dns()` | 在 PacketIn 中识别 DNS 请求并发回 response |
| `build_response()` | 根据 DNS query bytes 构造 DNS response bytes |
| `_build_a_answer()` | 构造 A record answer |
| `_build_ipv4_udp_frame()` | 构造以太网 + IPv4 + UDP + DNS 响应帧 |

设计选择：

- DNS 编解码使用 Python `struct` 手写，避免依赖不同版本 os-ken 是否提供 DNS packet 类。
- `build_response()` 是纯函数式核心逻辑，可以在 Windows 本地单元测试。
- `handle_dns()` 才依赖 os-ken 的 packet 对象，用于真实 controller 环境。

### 2.3 修改文件：`controller.py`

修改点 1：引入 DNS 模块。

```python
from dns_server import DNSServer
```

修改点 2：初始化 DNS server。

```python
self.dns_server = DNSServer()
```

修改点 3：switch 加入时安装 UDP/53 PacketIn flow。

```python
self.dns_server.install_packetin_flow(
    datapath,
    self.ofctls[datapath.id],
    ether_types,
    inet,
    VLANID_NONE,
)
```

这条 flow 的核心匹配条件是：

```text
dl_type = IPv4
nw_proto = UDP
tp_dst = 53
priority = 2000
actions = output:CONTROLLER
```

它的优先级高于普通 forwarding flow 的 `100`，因此 DNS 请求会先进入 controller。

修改点 4：在非 DHCP PacketIn 中分发 DNS。

当前逻辑：

```python
def _handle_non_dhcp_packet(self, datapath, in_port, pkt):
    pkt_arp = pkt.get_protocol(arp.arp)
    if pkt_arp:
        self._handle_arp(datapath, in_port, pkt_arp)
        return
    if self.dns_server.handle_dns(datapath, in_port, pkt):
        return
```

含义：

- ARP 仍然优先处理。
- 不是 ARP 时，尝试按 DNS 请求处理。
- 如果不是 DNS 请求，函数直接返回，不影响其他协议。

修改点 5：ARP 查询 DNS 虚拟 IP 时直接回复。

当 host 询问 `192.168.1.1` 的 MAC 时，controller 直接返回 `server_mac`：

```python
if self.dns_server.is_dns_ip(pkt_arp.dst_ip):
    ofctl.send_arp(
        arp.ARP_REPLY,
        VLANID_NONE,
        pkt_arp.src_mac,
        self.dns_server.server_mac,
        pkt_arp.dst_ip,
        pkt_arp.src_ip,
        pkt_arp.src_mac,
        datapath.ofproto.OFPP_CONTROLLER,
        in_port,
    )
```

这样 host 会认为 `192.168.1.1` 是一个真实存在的 DNS server。

### 2.4 修改文件：`dhcp.py`

修改点：DHCP 下发的 DNS server 改为 controller DNS。

```python
dns = '192.168.1.1'
```

这样 DHCP ACK 中的 DNS option 会把 `192.168.1.1` 下发给 host。

注意：

- 当前 DNS 测试脚本使用静态 IP，不依赖 DHCP。
- 修改 DHCP DNS option 是为了让最终 Demo 更自然：host 通过 DHCP 获得 IP 的同时，也获得 controller-hosted DNS server。

### 2.5 新增文件：`tests/test_dns_server.py`

文件作用：不依赖 Mininet 和 os-ken 的本地单元测试。

覆盖内容：

- 已知 A record 查询返回正确 IP。
- 未知域名返回 NXDOMAIN。
- 不支持的 query type 不返回 answer。

运行命令：

```powershell
python -m unittest tests.test_dns_server -v
```

预期结果：

```text
Ran 3 tests
OK
```

### 2.6 新增文件：`tests/dns_test/test_network.py`

文件作用：在 Mininet 中验证 controller DNS responder。

拓扑：

```text
h1 --- s1 --- h2
```

IP：

| Host | IP |
| --- | --- |
| h1 | `192.168.1.2/24` |
| h2 | `192.168.1.3/24` |
| DNS server | `192.168.1.1` |

测试内容：

- `web.cs305.local` 应解析为 `192.168.1.3`。
- `h1.cs305.local` 应解析为 `192.168.1.2`。
- `missing.cs305.local` 应返回 NXDOMAIN。
- `pingAll()` 应保持成功。
- flow table 应出现 UDP/53 到 controller 的规则。

---

## 3. 当前你接下来应该怎么操作

### Step 1：确认分支和工作区

在项目根目录运行：

```powershell
git status --short --branch --untracked-files=all
```

预期当前分支是：

```text
## bonus_4_DNS_NAT
```

如果不是，切换：

```powershell
git switch bonus_4_DNS_NAT
```

### Step 2：先在本地跑 DNS 单元测试

在 Windows 当前环境中运行：

```powershell
python -m unittest tests.test_dns_server -v
```

预期：

```text
test_answer_known_a_record ... ok
test_unknown_name_returns_nxdomain ... ok
test_unsupported_query_type_has_no_answer ... ok

Ran 3 tests
OK
```

这个测试只验证 DNS 报文构造逻辑，不需要 Mininet。

### Step 3：把分支同步到 Linux / Mininet 环境

如果你是在 Windows 开发、Linux VM 测试，可以先提交或直接同步文件。推荐提交前检查：

```powershell
git status --short --branch --untracked-files=all
```

本次 DNS 相关文件至少包括：

```text
controller.py
dhcp.py
dns_server.py
dns_records.json
tests/test_dns_server.py
tests/dns_test/test_network.py
docs/bonus_4.md
```

如果准备提交：

```powershell
git add controller.py dhcp.py dns_server.py dns_records.json tests/test_dns_server.py tests/dns_test/test_network.py docs/bonus_4.md
git commit -m "Implement controller-hosted DNS responder"
```

注意：不要把 `__pycache__` 或 `.pyc` 文件提交进去。

### Step 4：Linux 环境准备

进入 Linux VM 后：

```bash
cd /path/to/CS305-26Spring-Project
git switch bonus_4_DNS_NAT
```

激活环境：

```bash
conda activate cs305
```

确认 os-ken 可用：

```bash
osken-manager --version
```

确认 Mininet 可用：

```bash
sudo mn --test pingall
```

清理旧拓扑：

```bash
sudo mn -c
```

### Step 5：启动 controller

打开第一个终端，在项目根目录运行：

```bash
cd /path/to/CS305-26Spring-Project
conda activate cs305
osken-manager --observe-links controller.py
```

保持这个终端不要关闭。

重点观察：

- controller 是否启动成功。
- 是否有 `dns_server.py` 相关 import 错误。
- switch 加入时是否有 flow install 报错。

### Step 6：运行 DNS Mininet 测试

打开第二个终端：

```bash
cd /path/to/CS305-26Spring-Project
conda activate cs305
sudo mn -c
sudo env "PATH=$PATH" python tests/dns_test/test_network.py
```

预期输出中应包含：

```text
===== DNS bonus test results =====

[PASS] web.cs305.local resolves to 192.168.1.3
[PASS] h1.cs305.local resolves to 192.168.1.2
[PASS] missing.cs305.local returns NXDOMAIN
[PASS] normal shortest-path forwarding still works

===== Summary: 4/4 checks passed =====
```

还应看到 DNS flow：

```text
tp_dst=53 actions=CONTROLLER
```

不同 OVS 版本打印格式可能略有不同，只要能看出 UDP/53 被送到 controller 即可。

### Step 7：手动验证 DNS 查询

如果自动测试失败，可以进入 Mininet CLI 手动测试。临时把测试脚本里的 `net.stop()` 前加 CLI，或者复制 `tests/dns_test/test_network.py` 里的 query 逻辑。

也可以在 host 中运行一个 Python UDP DNS query：

```bash
h1 python3 - <<'PY'
import socket
import struct

def encode_qname(name):
    payload = b""
    for part in name.split("."):
        payload += bytes([len(part)]) + part.encode("ascii")
    return payload + b"\x00"

query_id = 0x3054
header = struct.pack("!HHHHHH", query_id, 0x0100, 1, 0, 0, 0)
question = encode_qname("web.cs305.local") + struct.pack("!HH", 1, 1)

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.settimeout(3)
sock.sendto(header + question, ("192.168.1.1", 53))
data, addr = sock.recvfrom(512)

print("response from", addr)
print("length", len(data))
print("hex", data.hex())
print("contains 192.168.1.3:", socket.inet_aton("192.168.1.3") in data)
PY
```

预期：

```text
contains 192.168.1.3: True
```

### Step 8：验证 DHCP 是否下发 DNS server

这个不是 DNS Mininet 测试的必要条件，但 Demo 时有帮助。

运行 DHCP 测试后，在 Mininet CLI 中查看 host DNS 配置：

```bash
h1 cat /etc/resolv.conf
```

理想情况下能看到：

```text
nameserver 192.168.1.1
```

如果看不到，也不一定代表 DNS responder 失败，因为当前 DNS 测试脚本是直接向 `192.168.1.1:53` 发送 query。

---

## 4. 故障排查

### 4.1 DNS 查询超时

优先检查 flow table：

```bash
sudo ovs-ofctl -O OpenFlow10 dump-flows s1
```

需要看到 UDP/53 到 controller 的 flow。

如果没有：

- 检查 controller 是否在 switch enter 时调用 `install_packetin_flow()`。
- 检查 controller 终端是否有 `set_flow()` 参数错误。
- 检查 `ofctl_utilis.py` 中 OpenFlow 1.0 的 `set_flow()` 是否支持 `tp_dst`。

### 4.2 ARP 找不到 `192.168.1.1`

在 Mininet CLI 中运行：

```bash
h1 arp -n
```

如果没有 `192.168.1.1`，尝试：

```bash
h1 arping -c 1 192.168.1.1
h1 arp -n
```

如果仍然没有：

- 检查 `_handle_arp()` 中 `self.dns_server.is_dns_ip(pkt_arp.dst_ip)` 是否命中。
- 检查 `dns_records.json` 中的 `server_ip` 是否为 `192.168.1.1`。
- 检查 controller 是否从项目根目录启动，否则可能读取不到 `dns_records.json`。

### 4.3 DNS response 返回了，但 IP 不正确

检查：

```bash
cat dns_records.json
```

确认：

```json
"web.cs305.local": "192.168.1.3"
```

如果修改过记录，需要重启 controller，因为 `DNSServer` 在初始化时读取记录文件。

### 4.4 `pingAll()` 失败

这通常不是 DNS 模块本身的问题，而是 host discovery 或 shortest path switching 没稳定。

检查：

```bash
dpctl dump-flows
```

确认普通目的 MAC forwarding flow 仍然存在。

也可以多发送几轮 gratuitous ARP：

```bash
h1 arping -c 2 -A -I h1-eth0 192.168.1.2
h2 arping -c 2 -A -I h2-eth0 192.168.1.3
pingall
```

### 4.5 Windows 本地 import `os_ken` 失败

这是正常的。Windows 当前环境不一定安装 os-ken。

本地只跑：

```powershell
python -m unittest tests.test_dns_server -v
```

真实 controller 验证必须在 Linux / Mininet / os-ken 环境中跑。

---

## 5. NAT 后续设计

NAT 尚未实现。下面是后续实现方案，等 DNS 在 Mininet 中确认通过后再开始。

### 5.1 NAT 最小目标

做一个最小 SNAT，不做完整复杂 NAT。

拓扑建议：

```text
h1 ---\
h2 ---- s1 ---- s2 ---- h_ext
```

地址规划：

| 节点 | 地址 |
| --- | --- |
| h1 | `192.168.1.2/24` |
| h2 | `192.168.1.3/24` |
| NAT inside gateway | `192.168.1.1` |
| NAT public IP | `10.0.0.1` |
| h_ext | `10.0.0.2/24` |

目标：

- h1 访问 `10.0.0.2` 上的 HTTP 服务。
- h_ext 看到来源是 `10.0.0.1`，不是 `192.168.1.2`。
- 回包能被改写回 h1。

### 5.2 NAT 可行性检查

当前 controller 使用 OpenFlow 1.0：

```python
OFP_VERSIONS = [ofproto_v1_0.OFP_VERSION]
```

实现 NAT 前，必须在 Linux 环境确认 OpenFlow 1.0 parser 是否支持地址和端口改写 action：

```bash
python - <<'PY'
from os_ken.ofproto import ofproto_v1_0_parser as p
print([x for x in dir(p) if "Action" in x or "Set" in x])
PY
```

重点确认是否存在：

- `OFPActionSetDlSrc`
- `OFPActionSetDlDst`
- `OFPActionSetNwSrc`
- `OFPActionSetNwDst`
- `OFPActionSetTpSrc`
- `OFPActionSetTpDst`

如果 `nw_src/nw_dst/tp_src/tp_dst` 改写 action 不可用，NAT 实现难度会明显升高，不建议继续做 NAT。

### 5.3 NAT 代码结构建议

新增 `nat.py`：

```python
class NAT:
    def __init__(self):
        self.inside_gateway_ip = "192.168.1.1"
        self.public_ip = "10.0.0.1"
        self.gateway_mac = "7e:49:b3:f0:f9:98"
        self.next_port = 40000
        self.mappings = {}
```

建议接口：

```python
def is_gateway_ip(self, ip):
    ...

def handle_arp(self, datapath, in_port, pkt_arp, ofctl):
    ...

def handle_nat_packet(self, datapath, in_port, pkt):
    ...

def install_nat_flows(self, datapath, mapping):
    ...
```

### 5.4 NAT 验证方式

在 h_ext 上启动 HTTP server：

```bash
h_ext python3 -m http.server 80 --bind 10.0.0.2
```

在 h1 上访问：

```bash
h1 curl -sS --connect-timeout 2 http://10.0.0.2/
```

在 h_ext 抓包：

```bash
h_ext tcpdump -n -i h_ext-eth0 host 10.0.0.1
```

预期：

- h1 curl 成功。
- h_ext 抓包看到源 IP 是 `10.0.0.1`。
- flow table 中能看到出站和回程 NAT flow。

---

## 6. Demo 展示建议

DNS Demo 建议按这个顺序展示：

1. 展示 `dns_records.json`，说明 controller 托管静态 DNS 表。
2. 启动 controller：

```bash
osken-manager --observe-links controller.py
```

3. 运行 DNS 测试：

```bash
sudo env "PATH=$PATH" python tests/dns_test/test_network.py
```

4. 展示结果：

```text
Summary: 4/4 checks passed
```

5. 展示 flow table：

```bash
sudo ovs-ofctl -O OpenFlow10 dump-flows s1
```

6. 强调：

- DNS 是新增的 os-ken controller 功能。
- UDP/53 请求由 switch 送到 controller。
- controller 使用 PacketOut 返回 DNS response。
- 普通 shortest path forwarding 仍然正常。

---

## 7. 报告写法建议

可以在 report 中这样写：

```text
For Bonus 4, we implemented a controller-hosted DNS responder using os-ken.
The controller installs a high-priority OpenFlow rule to redirect UDP/53
packets to the controller. When a host sends a DNS A-record query to the
virtual DNS server 192.168.1.1, the controller parses the query, looks up
the domain name in dns_records.json, constructs a DNS response, and sends
it back using PacketOut. We verified the function in Mininet by resolving
web.cs305.local to 192.168.1.3, h1.cs305.local to 192.168.1.2, and checking
that an unknown domain returns NXDOMAIN. We also confirmed that pingAll still
works, showing that the DNS extension does not break shortest-path switching.
```

如果 NAT 没有完成，不要写“实现了 NAT”。可以写：

```text
We also analyzed NAT as a possible extension. Because NAT requires OpenFlow
address and port rewrite actions and connection state management, we kept it
as future work after completing the DNS responder.
```

---

## 8. 最终检查清单

DNS 合入或展示前，至少完成：

- [ ] `python -m unittest tests.test_dns_server -v` 通过。
- [ ] Linux 环境中 controller 能正常启动。
- [ ] `tests/dns_test/test_network.py` 输出 `Summary: 4/4 checks passed`。
- [ ] flow table 中能看到 UDP/53 到 controller 的 flow。
- [ ] 截图保存 DNS 测试结果。
- [ ] 截图保存 flow table。
- [ ] 报告中说明 DNS 设计、实现、测试拓扑和测试结果。

完成这些后，DNS 部分就可以作为 Bonus 4 的主要交付成果。
