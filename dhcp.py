
import time
import ipaddress #解析、验证和操作 IPv4 和 IPv6 地址/网段
from typing import Optional #允许变量为“空”的类型提示

from os_ken.lib import addrconv
from os_ken.lib.packet import packet
from os_ken.lib.packet import ethernet
from os_ken.lib.packet import ipv4
from os_ken.lib.packet import udp
from os_ken.lib.packet import dhcp


# ──────────────────────────────────────────────
# 1.配置项
# ──────────────────────────────────────────────
class Config:
    controller_macAddr = '7e:49:b3:f0:f9:99'   # SDN 控制器的虚拟 MAC
    dns          = '8.8.8.8'
    start_ip     = '192.168.1.2'
    end_ip       = '192.168.1.100'  # 测试时设置较小范围，方便验证地址耗尽
    netmask      = '255.255.255.0'
    lease_time   = 86400       # 租期：1 天（秒）
    offer_timeout = 10            # OFFER 待确认超时（秒），RFC 建议值


# ──────────────────────────────────────────────
# 2.DHCP 服务器主类
# ──────────────────────────────────────────────
class DHCPServer:
    """
    状态表职责：
      pending_offers : mac → {ip, timestamp}
          OFFER 已发出但尚未收到 REQUEST 确认的"意向预留"
          超过 offer_timeout 秒后自动释放
      ip_pool        : mac → ip
          已通过 REQUEST/ACK 确认的正式租约
      lease_expiry   : ip  → float (unix timestamp)
          租约到期时间；超过后 _reclaim_expired() 回收
      bad_ips        : set of ip (str)
          客户端 DECLINE 过的地址，永不再分配（RFC 2131 §3.1.5）
    """

    #2.1 类级别状态 
    hardware_addr = Config.controller_macAddr
    start_ip      = Config.start_ip
    end_ip        = Config.end_ip
    netmask       = Config.netmask
    dns           = Config.dns
    lease_time    = Config.lease_time
    offer_timeout = Config.offer_timeout

    pending_offers: dict = {}   # mac → {"ip": str, "timestamp": float}
    ip_pool:        dict = {}   # mac → ip str
    lease_expiry:   dict = {}   # ip  → float
    bad_ips:        set  = set()  # 永久黑名单

    # 派生网络对象（类初始化时计算一次）
    # strict=False：不限制传入的 IP 必须是该网段的第一个标准网络号
    _network = ipaddress.IPv4Network(
        f'{Config.start_ip}/{Config.netmask}', strict=False
    )
    # 网关取网络地址 +1
    _gateway_ip = str(_network.network_address + 1)
    # 服务器标识符（Option 54）等于网关 IP，方便客户端识别
    server_identifier = _gateway_ip

    '''
    ══════════════════════════════════════════
    # 2.2工具方法
    _build_ip_range: 返回地址池范围内所有可用 IP 的有序列表。
    _is_expired: 判断某个 IP 对应的租约是否已超时。
    _reclaim_expired: 回收已过期的正式租约以及超时未确认的offer
    _in_use_ips: 返回当前所有被占用的 IP 集合（包含正式租约与意向预留）。
    _allocate_ip: 自动回收过期资源并执行原子性分配，为客户端安全地获取或预留一个可用 IP。
    reset: 重置所有状态（测试用），确保测试之间相互独立
    # ══════════════════════════════════════════
    '''

    @classmethod
    def reset(cls):
        """重置所有状态（测试用），确保测试之间相互独立"""
        cls.pending_offers.clear()
        cls.ip_pool.clear()
        cls.lease_expiry.clear()
        cls.bad_ips.clear()
        print("[RESET] DHCP server state cleared")

    @classmethod
    def _build_ip_range(cls) -> list:
        """返回地址池范围内所有可用 IP 的有序列表。"""
        start = int(ipaddress.IPv4Address(cls.start_ip))
        end   = int(ipaddress.IPv4Address(cls.end_ip))
        return [str(ipaddress.IPv4Address(i)) for i in range(start, end + 1)]

    @classmethod
    def _is_expired(cls, ip: str) -> bool:
        """判断某个 IP 对应的租约是否已超时。"""
        expiry = cls.lease_expiry.get(ip)
        return expiry is not None and time.time() > expiry

    @classmethod
    def _reclaim_expired(cls):
        """
        RFC 2131 §4.4.5 — 服务器必须回收超时租约。
        同时清理：
          1. ip_pool 中租期已过的正式租约
          2. pending_offers 中超过 offer_timeout 未确认的意向预留
        两者都清理完毕后，IP 才真正重新可用。
        """
        now = time.time()

        # 1. 回收过期正式租约
        expired_macs = [
            mac for mac, ip in cls.ip_pool.items() 
            if cls._is_expired(ip) #筛选条件
        ]
        for mac in expired_macs:
            ip = cls.ip_pool.pop(mac)
            cls.lease_expiry.pop(ip, None)
            print(f"[RECLAIM] 回收过期租约 mac={mac} ip={ip}")

        # 2. 回收超时未确认的 pending offer
        # RFC 2131 没有规定超时时间，实现上通常取 10 秒
        expired_offer_macs = [
            mac for mac, info in cls.pending_offers.items()
            if now - info["timestamp"] > cls.offer_timeout
        ]
        for mac in expired_offer_macs:
            info = cls.pending_offers.pop(mac)
            print(f"[RECLAIM] 回收超时 OFFER mac={mac} ip={info['ip']}")

    @classmethod
    def _in_use_ips(cls) -> set:
        """返回当前所有"被占用"的 IP（含正式租约 + 意向预留）。"""
        pooled  = set(cls.ip_pool.values())
        pending = {info["ip"] for info in cls.pending_offers.values()}
        return pooled | pending

    @classmethod
    def _allocate_ip(cls, client_mac: str) -> Optional[str]:
        """
        原子性分配 IP：
          1. 先回收过期资源
          2. 已有正式租约 → 续租同一 IP（RENEWING/REBINDING 路径在上层处理，
             此处为 INIT-REBOOT 或再次 DISCOVER ）
          3. 已有 pending offer → 返回同一 IP
          4. 从地址池顺序分配第一个空闲 IP，并立即写入 pending_offers
             （检查与标记在同一步骤内完成，避免竞争条件）
          5. 地址耗尽 → 返回 None

        "原子 reservation"关键：步骤 4 中找到可用 IP 后，立刻
        cls.pending_offers[client_mac] = {...}，不依赖调用方再次写入。
        这样同一个 IP 不会被两次 DISCOVER 并发拿到。
        """
        
        # 回收过期资源
        cls._reclaim_expired()

        # 已有正式租约
        if client_mac in cls.ip_pool:
            return cls.ip_pool[client_mac]

        # 已有 pending offer（幂等：重复 DISCOVER 不重新分配）
        if client_mac in cls.pending_offers:
            return cls.pending_offers[client_mac]["ip"]

        # 分配新 IP（原子 reservation）
        in_use = cls._in_use_ips()
        for ip in cls._build_ip_range():
            if ip not in in_use and ip not in cls.bad_ips:
                # ← 检查与标记在同一步骤，避免竞争
                cls.pending_offers[client_mac] = {
                    "ip":        ip,
                    "timestamp": time.time(),
                }
                print(f"[ALLOCATE] 预留 ip={ip} → mac={client_mac}")
                return ip

        return None  # 地址池耗尽

    '''
    # ══════════════════════════════════════════
    # 2.3解析 DHCP 选项
    _get_option: 从 DHCP 报文的选项列表中提取指定 tag 的原始字节值。
    _get_msg_type: 提取 Option 53 选项，获取 DHCP 消息类型。
    _get_requested_ip: 提取 Option 50 选项，获取客户端请求的 IP 地址。
    _get_server_id: 提取 Option 54 选项，获取 DHCP 服务器的标识符（IP地址）。
    # ══════════════════════════════════════════
    '''

    @staticmethod
    def _get_option(dhcp_pkt, tag: int):
        """从 DHCP 报文的 option_list 中提取指定 tag 的原始字节值。"""
        for opt in dhcp_pkt.options.option_list:
            if opt.tag == tag:
                return opt.value
        return None

    @classmethod
    def _get_msg_type(cls, dhcp_pkt) -> Optional[int]:
        """提取 Option 53 消息类型。"""
        val = cls._get_option(dhcp_pkt, dhcp.DHCP_MESSAGE_TYPE_OPT)
        return val[0] if val else None

    @classmethod
    def _get_requested_ip(cls, dhcp_pkt) -> Optional[str]:
        """提取 Option 50 (Requested IP Address)。"""
        val = cls._get_option(dhcp_pkt, dhcp.DHCP_REQUESTED_IP_ADDR_OPT)
        return addrconv.ipv4.bin_to_text(val) if val else None

    @classmethod
    def _get_server_id(cls, dhcp_pkt) -> Optional[str]:
        """提取 Option 54 (Server Identifier)。"""
        val = cls._get_option(dhcp_pkt, dhcp.DHCP_SERVER_IDENTIFIER_OPT)
        return addrconv.ipv4.bin_to_text(val) if val else None

    '''
    # ══════════════════════════════════════════
    # 2.4构建回复报文
    _build_options: 构建并返回 DHCP 回复所需的标准选项列表（包含消息类型、掩码、网关、DNS、租期和服务器标识）。
    _build_reply_pkt: 封装并生成完整的带有以太网、IPv4 和 UDP 层的 DHCP 回复报文（用于 OFFER 或 ACK），支持根据场景选择单播或广播发送。
    _build_nak_pkt: 封装并生成符合 RFC 2131 规范的 DHCPNAK 报文，将分配地址置为 0.0.0.0 并强制以广播形式发送，用于拒绝客户端的请求。
    # ══════════════════════════════════════════
    '''

    @classmethod
    def _build_options(cls, msg_type: int) -> dhcp.options:
        """
        构建 DHCP 回复选项列表。
        使用 ipaddress 模块计算网关
        Server Identifier (Option 54) 填写本服务器 IP。
        """
        return dhcp.options(option_list=[
            dhcp.option(
                tag=dhcp.DHCP_MESSAGE_TYPE_OPT,
                value=bytes([msg_type]),
            ),
            dhcp.option(
                tag=dhcp.DHCP_SUBNET_MASK_OPT,
                value=addrconv.ipv4.text_to_bin(cls.netmask),
            ),
            dhcp.option(
                tag=dhcp.DHCP_GATEWAY_ADDR_OPT,
                value=addrconv.ipv4.text_to_bin(cls._gateway_ip),
            ),
            dhcp.option(
                tag=dhcp.DHCP_DNS_SERVER_ADDR_OPT,
                value=addrconv.ipv4.text_to_bin(cls.dns),
            ),
            dhcp.option(
                tag=dhcp.DHCP_IP_ADDR_LEASE_TIME_OPT,
                value=cls.lease_time.to_bytes(4, byteorder='big'),
            ),
            dhcp.option(
                tag=dhcp.DHCP_SERVER_IDENTIFIER_OPT,
                value=addrconv.ipv4.text_to_bin(cls.server_identifier),
            ),
        ])

    @classmethod
    def _build_reply_pkt(cls, client_mac: str, xid: int,
                         offered_ip: str, msg_type: int,
                         unicast_ip: Optional[str] = None) -> packet.Packet:
        """
        封装 DHCP 回复报文（OFFER / ACK）。

        unicast_ip：若不为 None，则单播到该 IP（用于 RENEWING/REBINDING）；
                    否则广播（用于 DISCOVER / SELECTING / INIT-REBOOT）。
        RFC 2131 §4.1：
          - ciaddr == 0 且 broadcast flag 置位 → 广播
          - ciaddr 有效 → 可单播（RENEWING）
        """
        use_broadcast = unicast_ip is None
        dst_mac = 'ff:ff:ff:ff:ff:ff' if use_broadcast else client_mac
        dst_ip  = '255.255.255.255'    if use_broadcast else unicast_ip

        dhcp_pkt = dhcp.dhcp(
            op=dhcp.DHCP_BOOT_REPLY,
            chaddr=client_mac,
            siaddr=cls._gateway_ip,
            yiaddr=offered_ip,
            xid=xid,
            boot_file='',
            options=cls._build_options(msg_type),
        )
        pkt = packet.Packet()
        pkt.add_protocol(ethernet.ethernet(
            src=cls.hardware_addr, dst=dst_mac, ethertype=0x0800))
        pkt.add_protocol(ipv4.ipv4(
            src=cls._gateway_ip, dst=dst_ip, proto=17))
        pkt.add_protocol(udp.udp(src_port=67, dst_port=68))
        pkt.add_protocol(dhcp_pkt)
        return pkt

    @classmethod
    def _build_nak_pkt(cls, client_mac: str, xid: int) -> packet.Packet:
        """
        构建 DHCPNAK 报文。
        RFC 2131 §4.3.2：
          - msg type = DHCPNAK
          - yiaddr 必须为 0.0.0.0（不分配地址）
          - 必须广播发送（客户端可能还没有 IP）
        """
        options = dhcp.options(option_list=[
            dhcp.option(
                tag=dhcp.DHCP_MESSAGE_TYPE_OPT,
                value=bytes([6]),  # DHCPNAK = 6
            ),
            dhcp.option(
                tag=dhcp.DHCP_SERVER_IDENTIFIER_OPT,
                value=addrconv.ipv4.text_to_bin(cls.server_identifier),
            ),
        ])
        dhcp_pkt = dhcp.dhcp(
            op=dhcp.DHCP_BOOT_REPLY,
            chaddr=client_mac,
            siaddr=cls._gateway_ip,
            yiaddr='0.0.0.0',          # RFC 要求：NAK 不填 yiaddr
            xid=xid,
            boot_file='',
            options=options,
        )
        pkt = packet.Packet()
        pkt.add_protocol(ethernet.ethernet(
            src=cls.hardware_addr, dst='ff:ff:ff:ff:ff:ff', ethertype=0x0800))
        pkt.add_protocol(ipv4.ipv4(
            src=cls._gateway_ip, dst='255.255.255.255', proto=17))
        pkt.add_protocol(udp.udp(src_port=67, dst_port=68))
        pkt.add_protocol(dhcp_pkt)
        return pkt

    '''
    # ══════════════════════════════════════════
    # 2.5 RFC 2131 状态处理
    _handle_discover: 处理客户端广播的 DISCOVER 报文，为其预留 IP 并返回相应的 OFFER 回复报文。
    _handle_request: 处理 REQUEST 报文，自动识别并处理 SELECTING（选择确认）、INIT-REBOOT（重启恢复）、RENEWING（续租单播）和 REBINDING（续租广播）四种不同状态下的续租与确认逻辑。
    _handle_release: 处理客户端主动发送的 RELEASE 报文，立即释放对应的 IP 租约并将地址重新放回可用地址池。
    _handle_decline: 处理客户端发送的 DECLINE 冲突拒绝报文，将冲突的 IP 移出分配列表并加入 bad_ips 黑名单，防止该 IP 被再次分配。
    # ══════════════════════════════════════════
    '''

    @classmethod
    def _handle_discover(cls, pkt) -> Optional[packet.Packet]:
        """
        RFC 2131 §3.1.1 — INIT → SELECTING
        客户端广播 DISCOVER，服务器回复 OFFER。

        状态迁移：INIT ──DISCOVER──► (server) 发送 OFFER
        服务器此时仅"预留"IP（pending_offers），尚未正式租出。
        """
        eth_pkt    = pkt.get_protocol(ethernet.ethernet)
        dhcp_pkt   = pkt.get_protocol(dhcp.dhcp)
        client_mac = eth_pkt.src

        offered_ip = cls._allocate_ip(client_mac)
        # _allocate_ip 内部已完成原子 reservation，写入 pending_offers
        if offered_ip is None:
            print(f"[DISCOVER] 地址池耗尽，无法回复 mac={client_mac}")
            return None

        print(f"[OFFER] mac={client_mac} offered_ip={offered_ip}")
        # OFFER 必须广播
        return cls._build_reply_pkt(
            client_mac, dhcp_pkt.xid, offered_ip, dhcp.DHCP_OFFER
        )

    @classmethod
    def _handle_request(cls, pkt) -> Optional[packet.Packet]:
        """
        RFC 2131 §3.1.2 / §4.3.2 — REQUEST 的四种语义

        通过报文字段区分：
        ┌─────────────┬────────┬──────────┬──────────────┐
        │ 状态         │ciaddr  │ Option50 │ Option54     │
        ├─────────────┼────────┼──────────┼──────────────┤
        │ SELECTING   │  0     │  有       │ 有（本服务器）│
        │ INIT-REBOOT │  0     │  有       │ 无           │
        │ RENEWING    │ 非 0   │  无       │ 无（单播）    │
        │ REBINDING   │ 非 0   │  无       │ 无（广播）    │
        └─────────────┴────────┴──────────┴──────────────┘
        """
        eth_pkt      = pkt.get_protocol(ethernet.ethernet)
        dhcp_pkt     = pkt.get_protocol(dhcp.dhcp)
        client_mac   = eth_pkt.src
        ciaddr       = dhcp_pkt.ciaddr       # 客户端当前 IP（0.0.0.0 若无）
        xid          = dhcp_pkt.xid

        requested_ip  = cls._get_requested_ip(dhcp_pkt)   # Option 50
        server_id_opt = cls._get_server_id(dhcp_pkt)      # Option 54

        # ── SELECTING（含 Server Identifier）──────────────────
        # RFC 2131 §4.3.2：客户端选定某台服务器，发送含 Option54 的 REQUEST
        if ciaddr == '0.0.0.0' and requested_ip and server_id_opt:
            # 1. 检查 Option54 是否指向本服务器；若不是，静默忽略
            #    （避免多台 DHCP Server 同时 ACK 同一 REQUEST）
            if server_id_opt != cls.server_identifier:
                print(f"[REQUEST/SELECTING] Option54={server_id_opt} "
                      f"≠ 本服务器 {cls.server_identifier}，忽略")
                # 同时释放本服务器之前给该 MAC 的 pending offer
                cls.pending_offers.pop(client_mac, None)
                return None

            # 2. 验证 requested_ip 是否与本服务器 pending_offers 中记录一致
            pending = cls.pending_offers.get(client_mac)
            if pending is None or pending["ip"] != requested_ip:
                print(f"[REQUEST/SELECTING] NAK: mac={client_mac} "
                      f"requested={requested_ip} pending={pending}")
                return cls._build_nak_pkt(client_mac, xid)

            # 3. 验证 requested_ip 不在地址池外
            if not cls._in_pool(requested_ip):
                print(f"[REQUEST/SELECTING] NAK: {requested_ip} 不在地址池")
                cls.pending_offers.pop(client_mac, None)
                return cls._build_nak_pkt(client_mac, xid)

            # 4. 正式创建租约 → ACK
            return cls._confirm_lease(client_mac, xid, requested_ip,
                                      unicast_ip=None)

        # ── INIT-REBOOT（有 Option50，无 Option54，ciaddr=0）─────
        # RFC 2131 §3.2：客户端重启后尝试续用之前的 IP
        if ciaddr == '0.0.0.0' and requested_ip and not server_id_opt:
            existing_ip = cls.ip_pool.get(client_mac)

            # 情况 A：服务器有该 MAC 的租约且 IP 匹配 → ACK
            if existing_ip == requested_ip and not cls._is_expired(requested_ip):
                print(f"[REQUEST/INIT-REBOOT] ACK: mac={client_mac} "
                      f"ip={requested_ip}")
                # 延长租期
                cls.lease_expiry[requested_ip] = time.time() + cls.lease_time
                return cls._build_reply_pkt(
                    client_mac, xid, requested_ip, dhcp.DHCP_ACK
                )

            # 情况 B：请求的 IP 已被别人用 → NAK
            if requested_ip in set(cls.ip_pool.values()):
                print(f"[REQUEST/INIT-REBOOT] NAK: {requested_ip} "
                      f"被其他客户端占用")
                return cls._build_nak_pkt(client_mac, xid)

            # 情况 C：服务器无该 MAC 记录 → NAK
            print(f"[REQUEST/INIT-REBOOT] NAK: 无 {client_mac} 的租约记录")
            return cls._build_nak_pkt(client_mac, xid)

        # ── RENEWING（ciaddr 有效，单播）& REBINDING（ciaddr 有效，广播）─
        # RFC 2131 §4.4.5：客户端在租期 T1/T2 时刻发送续租 REQUEST
        # 两者处理逻辑相同，区别在传输层（单播/广播）；SDN 层面已统一收包
        if ciaddr and ciaddr != '0.0.0.0':
            existing_ip = cls.ip_pool.get(client_mac)

            # 验证 ciaddr 确实属于该 MAC
            if existing_ip != ciaddr:
                print(f"[REQUEST/RENEWING] NAK: mac={client_mac} "
                      f"ciaddr={ciaddr} 不匹配记录 {existing_ip}")
                return cls._build_nak_pkt(client_mac, xid)

            # 验证租约未过期（REBINDING 时租约可能已完全失效）
            if cls._is_expired(ciaddr):
                print(f"[REQUEST/RENEWING] NAK: 租约已过期 ip={ciaddr}")
                cls.ip_pool.pop(client_mac, None)
                cls.lease_expiry.pop(ciaddr, None)
                return cls._build_nak_pkt(client_mac, xid)

            # 延长租约 → 单播 ACK（客户端已有 IP，RFC 允许单播）
            cls.lease_expiry[ciaddr] = time.time() + cls.lease_time
            print(f"[REQUEST/RENEWING] ACK: mac={client_mac} "
                  f"ip={ciaddr} 续租至 {cls.lease_expiry[ciaddr]:.0f}")
            return cls._build_reply_pkt(
                client_mac, xid, ciaddr, dhcp.DHCP_ACK,
                unicast_ip=ciaddr  # 单播回复
            )

        # 无法归类的 REQUEST → 静默忽略
        print(f"[REQUEST] 无法识别语义，忽略 mac={client_mac}")
        return None

    @classmethod
    def _handle_release(cls, pkt):
        """
        RFC 2131 §3.4 — 客户端主动释放租约。
        立即从 ip_pool / lease_expiry 中移除，IP 重新可用。
        """
        eth_pkt    = pkt.get_protocol(ethernet.ethernet)
        client_mac = eth_pkt.src
        ip = cls.ip_pool.pop(client_mac, None)
        if ip:
            cls.lease_expiry.pop(ip, None)
            print(f"[RELEASE] mac={client_mac} 释放 ip={ip}")
        cls.pending_offers.pop(client_mac, None)

    @classmethod
    def _handle_decline(cls, pkt):
        """
        RFC 2131 §3.1.5 — 客户端检测到地址冲突，发送 DECLINE。
        将该 IP 加入 bad_ips 黑名单，永不再分配。
        """
        eth_pkt    = pkt.get_protocol(ethernet.ethernet)
        dhcp_pkt   = pkt.get_protocol(dhcp.dhcp)
        client_mac = eth_pkt.src
        declined_ip = cls._get_requested_ip(dhcp_pkt)  # Option 50

        if declined_ip is None:
            # 也可从 pending_offers 推断
            pending = cls.pending_offers.get(client_mac)
            declined_ip = pending["ip"] if pending else None

        if declined_ip:
            # 从所有状态表中移除
            cls.pending_offers.pop(client_mac, None)
            # 若已写入 ip_pool（理论上不应，但防御性清理）
            for m, ip in list(cls.ip_pool.items()):
                if ip == declined_ip:
                    cls.ip_pool.pop(m)
            cls.lease_expiry.pop(declined_ip, None)
            # 加入黑名单
            cls.bad_ips.add(declined_ip)
            print(f"[DECLINE] mac={client_mac} 拒绝 ip={declined_ip}，"
                  f"加入 bad_ips 黑名单")
    '''
    # ══════════════════════════════════════════
    # 2.6 内部辅助
    _in_pool: 使用 ipaddress 模块判断指定的 IP 地址是否在合法的地址池范围内。
    _confirm_lease: 将临时预留（pending offer）正式升级为长期租约，记录到租约状态表并清除预留记录，最后返回相应的 ACK 确认报文。
    # ══════════════════════════════════════════
    '''

    @classmethod
    def _in_pool(cls, ip: str) -> bool:
        """使用 ipaddress 模块判断 IP 是否在合法地址池范围内。"""
        try:
            addr  = ipaddress.IPv4Address(ip)
            start = ipaddress.IPv4Address(cls.start_ip)
            end   = ipaddress.IPv4Address(cls.end_ip)
            return start <= addr <= end
        except ValueError:
            return False

    @classmethod
    def _confirm_lease(cls, client_mac: str, xid: int,
                       ip: str, unicast_ip: Optional[str]) -> packet.Packet:
        """
        将 pending offer 升级为正式租约，写入 ip_pool + lease_expiry，
        清除 pending_offers 条目，然后回复 DHCPACK。

        RFC 2131 §4.3.1：服务器在确认 REQUEST 后才正式记录租约。
        """
        cls.ip_pool[client_mac]    = ip
        cls.lease_expiry[ip]       = time.time() + cls.lease_time
        cls.pending_offers.pop(client_mac, None)
        expiry = cls.lease_expiry[ip]
        print(f"[ACK] mac={client_mac} ip={ip} "
              f"lease_expiry={expiry:.0f} (+{cls.lease_time}s)")
        return cls._build_reply_pkt(
            client_mac, xid, ip, dhcp.DHCP_ACK, unicast_ip=unicast_ip
        )

    '''
    # ══════════════════════════════════════════
    # 2.7 主入口
    handle_dhcp: DHCP 报文分发入口，负责识别报文的消息类型（DISCOVER、REQUEST、RELEASE、DECLINE）
    # ══════════════════════════════════════════
    '''

    @classmethod
    def handle_dhcp(cls, datapath, port, pkt):
        """
        DHCP 报文分发入口。
        对应 RFC 2131 §3 中描述的服务器端状态机。
        """
        dhcp_pkt = pkt.get_protocol(dhcp.dhcp)
        if dhcp_pkt is None:
            return

        msg_type = cls._get_msg_type(dhcp_pkt)
        if msg_type is None:
            return

        # ── DISCOVER ──────────────────────────
        # RFC 状态：客户端处于 INIT，广播寻找服务器
        if msg_type == dhcp.DHCP_DISCOVER:
            reply = cls._handle_discover(pkt)
            if reply:
                cls._send_packet(datapath, port, reply)

        # ── REQUEST ───────────────────────────
        # RFC 状态：SELECTING / INIT-REBOOT / RENEWING / REBINDING
        elif msg_type == dhcp.DHCP_REQUEST:
            reply = cls._handle_request(pkt)
            if reply:
                cls._send_packet(datapath, port, reply)

        # ── RELEASE ───────────────────────────
        # RFC 状态：客户端主动释放
        elif msg_type == dhcp.DHCP_RELEASE:
            cls._handle_release(pkt)

        # ── DECLINE ───────────────────────────
        # RFC 状态：客户端 ARP 探测发现冲突，拒绝使用已 ACK 的地址
        elif msg_type == dhcp.DHCP_DECLINE:
            cls._handle_decline(pkt)

        else:
            print(f"[DHCP] 未处理的消息类型 msg_type={msg_type}")
    
    '''
    # ══════════════════════════════════════════
    # 2.8 发包
    _send_packet: 通过 OpenFlow 的 PacketOut 消息，将构建好的 DHCP 回复报文序列化后从控制器确定的指定端口发送出去。
    # ══════════════════════════════════════════
    '''
    
    @classmethod
    def _send_packet(cls, datapath, port, pkt):
        """
        通过 OpenFlow PacketOut 将报文从指定端口发出。
        广播/单播的选择已在 _build_reply_pkt / _build_nak_pkt 中通过
        dst_mac / dst_ip 决定，本方法只负责 OF 层面的转发动作。
        """
        ofproto = datapath.ofproto
        parser  = datapath.ofproto_parser
        if isinstance(pkt, str):
            pkt = pkt.encode()
        pkt.serialize()
        data    = pkt.data
        actions = [parser.OFPActionOutput(port=port)]
        out = parser.OFPPacketOut(
            datapath=datapath,
            buffer_id=ofproto.OFP_NO_BUFFER,
            in_port=ofproto.OFPP_CONTROLLER,
            actions=actions,
            data=data,
        )
        datapath.send_msg(out)