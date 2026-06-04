# dhcp basic + bonus1 + bonus2 + others

## 1. environment setup
Environment Setup

Open **two terminals**.

### Terminal 1: controller

```bash
cd /home/w1369/computer_network/make_proj/CS305-26Spring-Project
sudo mn -c
/home/w1369/miniforge3/envs/cs305/bin/osken-manager --observe-links controller.py
```

This terminal is for:

- controller startup,
- path printouts,
- topology reaction,
- firewall rule installation behavior,
- DHCP log behavior.

### Terminal 2: Mininet test scripts

All test scripts are started here.

Before switching to a different topology, run:

```bash
sudo mn -c
```

## 2.dhcp basic

1. basic DHCP allocation with default configuration
2. DHCP allocation after changing `start_ip`, `end_ip`, and `netmask`
3. address pool exhaustion where only the first `n` hosts receive addresses.

### Main code to mention

- `dhcp.py`
- `controller.py`
- `tests/dhcp_test/test_network.py`
- `tests/dhcp_test/test_custom_config.py`
- `tests/dhcp_test/test_small_pool_exhaustion.py`


### 2.1 basic DHCP allocation with default configuration

#### Command

Terminal 2:

```bash
sudo env "PATH=$PATH" /home/w1369/miniforge3/envs/cs305/bin/python tests/dhcp_test/test_network.py
```

Inside Mininet CLI:

```text
h1 ifconfig h1-eth0
h2 ifconfig h2-eth0
h1 ping -c 2 192.168.1.3
exit
```

#### What to show

- `h1` gets a valid IP
- `h2` gets a valid IP
- `h1` can ping `h2`

#### What to say


### 2.2 DHCP allocation after changing `start_ip`, `end_ip`, and `netmask`

This demonstrates instruction item 2 for DHCP.

#### Restart controller with custom DHCP settings

Terminal 1:

```bash
sudo mn -c
DHCP_START_IP=10.10.0.10 DHCP_END_IP=10.10.0.12 DHCP_NETMASK=255.255.255.248 /home/w1369/miniforge3/envs/cs305/bin/osken-manager --observe-links controller.py
```

### Command

Terminal 2:

```bash
sudo env "PATH=$PATH" DHCP_START_IP=10.10.0.10 DHCP_END_IP=10.10.0.12 DHCP_NETMASK=255.255.255.248 /home/w1369/miniforge3/envs/cs305/bin/python tests/dhcp_test/test_custom_config.py
```

### What to show

- assigned IPs are in the range `10.10.0.10` to `10.10.0.12`
- subnet information changes with the new netmask
- communication still works


### 2.3 address pool exhaustion where only the first `n` hosts receive addresses.

This demonstrates instruction item 3 for DHCP.

#### Keep the same custom-config controller

### Command

Terminal 2:

```bash
sudo mn -c
sudo env "PATH=$PATH" DHCP_START_IP=10.10.0.10 DHCP_END_IP=10.10.0.12 DHCP_NETMASK=255.255.255.248 /home/w1369/miniforge3/envs/cs305/bin/python tests/dhcp_test/test_small_pool_exhaustion.py
```

### What to show

- only the first 3 hosts receive valid addresses
- the remaining hosts do not receive any address


## 3.dhcp bonus1

DHCP租期有效期功能

### OFFER timeout reclaim

```bash
sudo mn -c
/home/w1369/miniforge3/envs/cs305/bin/osken-manager --observe-links controller.py
sudo env "PATH=$PATH" /home/w1369/miniforge3/envs/cs305/bin/python tests/dhcp_test/test_offer_timeout.py
```

### Lease expiration and renewal

```bash
sudo mn -c
sudo env "PATH=$PATH" /home/w1369/miniforge3/envs/cs305/bin/python tests/dhcp_test/test_lease_expiry.py
```

## 4.dhcp bonus2

遵循RFC协议优化DHCP，杜绝IP地址重复分配

### Concurrent allocation and default pool exhaustion

```bash
sudo mn -c
sudo env "PATH=$PATH" /home/w1369/miniforge3/envs/cs305/bin/python tests/dhcp_test/test_concurrent_exhaust.py
```

```
                                    ┌─────────────┐
                                    │    INIT     │
                                    └──────┬──────┘
                                           │ DHCP DISCOVER (broadcast)
                                           │ 
                                    ┌──────▼──────┐
                                    │  SELECTING  │
                                    └──────┬──────┘
                                           │ DHCP OFFER (from server)
                                           │ DHCP REQUEST (broadcast, with server_id)
                                    ┌──────▼──────┐
                         ┌──────────┤  REQUESTING ├──────────┐
                         │          └─────────────┘          │
                      DHCPACK                             DHCPNAK
                         │                                   │
                  ┌──────▼──────┐                      ┌─────▼─────┐
                  │    BOUND    │                      │    INIT   │
                  └──────┬──────┘                      └───────────┘
                         │
            ┌────────────┼────────────┐
            │ T1 (50%)                │ T2 (87.5%)
            │                         │
      ┌─────▼──────┐             ┌────▼─────┐
      │  RENEWING  │             │ REBINDING│
      └─────┬──────┘             └────┬─────┘
            │                         │
      DHCPACK (unicast)        DHCPACK (broadcast)
            │                         │
      ┌─────▼──────┐            ┌─────▼─────┐
      │    BOUND   │            |   BOUND   │
      └────────────┘            └───────────┘
```

### 服务器端状态管理

服务器维护四个核心状态表：
- `pending_offers`：OFFER已发出但尚未收到REQUEST确认的"意向预留"，超过offer_timeout秒后自动释放
- `ip_pool`：已通过REQUEST/ACK确认的正式租约
- `lease_expiry`：租约到期时间；超过后_reclaim_expired()回收
- `bad_ips`：客户端DECLINE过的地址，永不再分配（RFC 2131 §3.1.5）

### 状态转换触发条件

| 客户端消息 | 服务器动作 | 状态变化 |
|-----------|-----------|---------|
| DISCOVER | 分配IP → 发送OFFER | INIT → pending_offers预留 |
| REQUEST (SELECTING) | 验证 → 发送ACK/NAK | pending_offers → ip_pool |
| **REQUEST (INIT-REBOOT)** | 验证历史租约 → ACK/NAK | 延长lease_expiry |
| **REQUEST (RENEWING)** | 验证 → 延长租约 | 更新lease_expiry |
| **RELEASE** | 立即释放 | ip_pool → 移除 |
| **DECLINE** | 加入黑名单 | bad_ips.add(ip) |

## 5.dhcp others

除了核心的DHCP功能外，服务器还实现了NAK报文构建、单播/广播选择等辅助功能，确保协议的完整性和健壮性。


### 5.1 NAK报文构建

构建符合RFC 2131规范的DHCPNAK报文，拒绝客户端的无效请求。

```python
@classmethod
def _build_nak_pkt(cls, client_mac: str, xid: int) -> packet.Packet:
    dhcp_pkt = dhcp.dhcp(
        op=dhcp.DHCP_BOOT_REPLY,
        chaddr=client_mac,
        yiaddr='0.0.0.0',  # RFC要求：NAK不填yiaddr
        xid=xid,
        options=dhcp.options(option_list=[
            dhcp.option(tag=dhcp.DHCP_MESSAGE_TYPE_OPT, value=bytes([6])),
            dhcp.option(tag=dhcp.DHCP_SERVER_IDENTIFIER_OPT, 
                       value=addrconv.ipv4.text_to_bin(cls.server_identifier)),
        ])
    )
    # 必须广播发送（客户端可能还没有IP）
    pkt.add_protocol(ethernet.ethernet(dst='ff:ff:ff:ff:ff:ff'))
    return pkt
```

### 5.2 单播/广播选择

根据客户端状态选择单播或广播发送回复报文。

```python
@classmethod
def _build_reply_pkt(cls, client_mac: str, xid: int, offered_ip: str, 
                     msg_type: int, unicast_ip: Optional[str] = None):
    use_broadcast = unicast_ip is None
    dst_mac = 'ff:ff:ff:ff:ff:ff' if use_broadcast else client_mac
    dst_ip  = '255.255.255.255'    if use_broadcast else unicast_ip
    # RENEWING时客户端已有IP，RFC允许单播
```
