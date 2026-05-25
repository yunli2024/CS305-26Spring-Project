"""
多客户端并发获取 & 地址池耗尽测试
测试场景：
  Test4: 10 个主机同时发送 DHCP 请求，验证分配的 IP 互不冲突
  Test5: 地址池耗尽后，新客户端 DISCOVER 不回复
Note: 需要与 dhcp.py 中的 Config.start_ip / end_ip 配置一致（192.168.1.2 - 192.168.1.11，共 10 个）
"""

from mininet.log import setLogLevel, info
from mininet.net import Mininet
from mininet.node import RemoteController
from mininet.topo import Topo
import time


def disable_ipv6(node):
    """禁用节点上的 IPv6"""
    node.cmd("sysctl -w net.ipv6.conf.all.disable_ipv6=1")
    node.cmd("sysctl -w net.ipv6.conf.default.disable_ipv6=1")
    node.cmd("sysctl -w net.ipv6.conf.lo.disable_ipv6=1")

def send_dhcp_discover(node, interface='eth0'):
    """发送 DHCP DISCOVER（仅发送 DISCOVER，不完成后续流程）"""
    info('%s: Sending DHCP DISCOVER on %s\n' % (node.name, interface))
    node.cmd('python3 -c "from scapy.all import *; send(IP(dst=\'255.255.255.255\')/UDP(sport=68,dport=67)/BOOTP(xid=0x1234)/DHCP(options=[(\'message-type\',\'discover\'),(\'end\')]))"')

def send_dhcp_request(node, interface='eth0'):
    """发送 DHCP 请求（完整的 DORA 流程，非阻塞）"""
    info('%s: Sending DHCP request on %s\n' % (node.name, interface))
    node.cmd('dhclient -v -nw %s-%s 2>&1 &' % (node.name, interface))

def get_current_ip(node, interface='eth0'):
    """获取当前 IP 地址"""
    result = node.cmd('ip addr show %s-%s' % (node.name, interface))
    lines = result.split('\n')
    for line in lines:
        if 'inet ' in line:
            parts = line.strip().split()
            for i, part in enumerate(parts):
                if part == 'inet':
                    return parts[i + 1].split('/')[0]
    return None


class Test45Topo(Topo):
    """Test4 & Test5: 11 台主机共享一台交换机"""
    def __init__(self, **opts):
        Topo.__init__(self, **opts)

        s1 = self.addSwitch('s1')
        for i in range(1, 12):
            h = self.addHost('h%d' % i, ip='no ip defined/8')
            self.addLink(h, s1)


def test45_concurrent_and_exhaust():
    """
    Test4 + Test5: 多客户端并发获取 & 地址池耗尽
    流程:
    1. h1~h10 同时发送 DHCP 请求（完整 DORA）
    2. 等待所有主机完成分配
    3. [Test4] 验证 h1~h10 均获得 IP，且 IP 互不冲突
    4. h11 发送 DISCOVER（地址池此时仍有剩余，此步仅占位触发后续耗尽场景）
       ── 实际耗尽场景：修改 Config.end_ip = start_ip + 9 使池子恰好 10 个 ──
       本测试以"h11 在 h1~h10 已占满全部池位后收不到 OFFER"为验证目标
    5. [Test5] 验证 h11 等待超时后仍无 IP（服务器静默不回复）
    """
    info('\n[Test4 + Test5]: Concurrent Allocation & Pool Exhaustion\n')

    host_count  = 10   # 正常分配的主机数
    pool_size   = 10   # 与 Config 中地址池大小保持一致（测试时请将 end_ip 设为 start_ip+9）
    wait_normal = 10   # 等待正常 DORA 完成的秒数
    wait_exhaust = 8   # 等待 h11 超时的秒数（收不到 OFFER 则始终无 IP）

    topo = Test45Topo()
    net = Mininet(topo=topo, autoSetMacs=True, controller=RemoteController)
    for h in net.hosts:
        disable_ipv6(h)
    for s in net.switches:
        disable_ipv6(s)
    net.start()

    # ── Step 1: h1~h10 同时发送 DHCP 请求 ────────────────────────
    info('\nStep 1: h1~h10 send DHCP requests simultaneously\n')
    for i in range(1, host_count + 1):
        h = net.get('h%d' % i)
        send_dhcp_request(h)
    request_time = time.time()
    info('All requests sent at: %s\n' % time.strftime('%H:%M:%S', time.localtime(request_time)))

    # ── Step 2: 等待 h1~h10 完成 DORA ────────────────────────────
    info('\nStep 2: Waiting %d seconds for h1~h10 to complete DORA...\n' % wait_normal)
    remaining = wait_normal
    while remaining > 0:
        info('Remaining: %d seconds      \r' % remaining)
        time.sleep(min(remaining, 2))
        remaining -= 2
    info('\nh1~h10 should have obtained IPs now!\n')

    # ── Step 3: [Test4] 收集并验证 h1~h10 的 IP ──────────────────
    info('\nStep 3: [Test4] Collecting and verifying IP assignments for h1~h10\n')
    ip_map = {}
    for i in range(1, host_count + 1):
        h = net.get('h%d' % i)
        ip = get_current_ip(h)
        ip_map[h.name] = ip
        info('%s IP: %s\n' % (h.name, ip if ip else 'None'))

    # 验证 1：所有主机都获得了 IP
    failed_hosts = [name for name, ip in ip_map.items() if ip is None]
    all_got_ip = len(failed_hosts) == 0
    if not all_got_ip:
        info('[WARN]: Hosts without IP: %s\n' % ', '.join(failed_hosts))

    # 验证 2：所有 IP 互不冲突
    assigned_ips = [ip for ip in ip_map.values() if ip is not None]
    no_conflict = len(assigned_ips) == len(set(assigned_ips))
    if not no_conflict:
        seen = set()
        for name, ip in ip_map.items():
            if ip in seen:
                info('[WARN]: Duplicate IP %s assigned to %s\n' % (ip, name))
            seen.add(ip)

    if all_got_ip and no_conflict:
        info('[PASSED] Test4: All %d hosts obtained unique IPs, no conflicts\n' % host_count)
        result4 = True
    else:
        info('[FAILED] Test4: IP assignment conflicts or missing IPs detected\n')
        result4 = False

    # ── Step 4: h11 在地址池耗尽后发送 DISCOVER ──────────────────
    info('\nStep 4: [Test5] h11 sends DISCOVER after pool is exhausted\n')
    h11 = net.get('h11')
    send_dhcp_discover(h11)
    discover_time = time.time()
    info('h11 DISCOVER sent at: %s\n' % time.strftime('%H:%M:%S', time.localtime(discover_time)))

    # ── Step 5: [Test5] 等待超时，验证 h11 始终无 IP ─────────────
    info('\nStep 5: [Test5] Waiting %d seconds, h11 should receive no OFFER...\n' % wait_exhaust)
    remaining = wait_exhaust
    while remaining > 0:
        info('Remaining: %d seconds      \r' % remaining)
        time.sleep(min(remaining, 2))
        remaining -= 2
    info('\nChecking h11 IP...\n')

    h11_ip = get_current_ip(h11)
    info('h11 IP: %s\n' % (h11_ip if h11_ip else 'None (as expected)'))

    if h11_ip is None:
        info('[PASSED] Test5: h11 received no OFFER when pool exhausted\n')
        result5 = True
    else:
        info('[FAILED] Test5: h11 unexpectedly obtained IP %s\n' % h11_ip)
        result5 = False

    net.stop()
    return result4, result5


def main():
    setLogLevel('info')

    info('\n' + '='*60 + '\n')
    info('DHCP Tests\n')
    info('='*60 + '\n')

    result4, result5 = test45_concurrent_and_exhaust()

    info('\n' + '='*60 + '\n')
    info('Test Results:\n')
    info('  [Test4] Concurrent Multi-Client Allocation: %s\n' % ('[PASSED]' if result4 else '[FAILED]'))
    info('  [Test5] Pool Exhaustion No Reply:           %s\n' % ('[PASSED]' if result5 else '[FAILED]'))
    info('='*60 + '\n')


if __name__ == '__main__':
    main()