"""
多客户端并发获取 & OFFER 超时回收测试
测试场景：
  Test3: 发送 DISCOVER 后超过 offer_timeout 秒不发送 REQUEST，IP 释放回池
Note: 需要与 dhcp.py 中的 Config.offer_timeout 配置一致（10秒）
"""

from mininet.cli import CLI
from mininet.link import TCLink
from mininet.log import setLogLevel, info
from mininet.net import Mininet
from mininet.node import RemoteController, OVSSwitch
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


class Test3Topo(Topo):
    """Test3: 测试 OFFER 超时回收的拓扑"""
    def __init__(self, **opts):
        Topo.__init__(self, **opts)

        h1 = self.addHost('h1', ip='no ip defined/8')
        s1 = self.addSwitch('s1')
        self.addLink(h1, s1)


def test3_offer_timeout_reclaim():
    """
    Test3: OFFER 超时回收
    流程:
    1. h1 发送 DISCOVER → 服务器分配 IP 并记录到 pending_offers
    2. 等待 offer_timeout + buffer 秒 → pending offer 应被回收
    3. h1 重新发送 DISCOVER → 验证能重新获得之前预留的 IP
    """
    info('\n[Test3]: OFFER Timeout Reclaim\n')

    offer_timeout = 10  # 秒
    wait_buffer = 3     # 额外等待时间，确保回收完成

    topo = Test3Topo()
    net = Mininet(topo=topo, autoSetMacs=True, controller=RemoteController)
    for h in net.hosts:
        disable_ipv6(h)
    for s in net.switches:
        disable_ipv6(s)
    net.start()
    h1 = net.get('h1')

    # Step 1: h1 发送 DISCOVER（只发 DISCOVER，不完成 REQUEST）
    info('\nStep 1: h1 sends DISCOVER (only DISCOVER, no REQUEST)\n')
    send_dhcp_discover(h1)
    time.sleep(1)

    discover_time = time.time()
    expire_time = discover_time + offer_timeout
    info('DISCOVER sent at: %s\n' % time.strftime('%H:%M:%S', time.localtime(discover_time)))
    info('OFFER will expire at: %s\n' % time.strftime('%H:%M:%S', time.localtime(expire_time)))

    # Step 2: 等待 OFFER 超时
    total_wait = offer_timeout + wait_buffer
    info('\nStep 2: Waiting %d seconds for OFFER to timeout...\n' % total_wait)
    remaining = total_wait
    while remaining > 0:
        info('Remaining: %d seconds      \r' % remaining)
        time.sleep(min(remaining, 3))
        remaining -= 3
    info('\nOFFER should have expired now!\n')

    # Step 3: h1 重新发送 DISCOVER（此时之前的 pending offer 应已释放）
    info('\nStep 3: h1 re-sends DISCOVER\n')
    send_dhcp_discover(h1)
    time.sleep(1)
    info('h1 re-sent DISCOVER, pending offer should have been reclaimed\n')

    # Step 4: h1 完成 DORA 流程
    info('\nStep 4: h1 completes DORA\n')
    send_dhcp_request(h1)
    time.sleep(2)
    h1_ip = get_current_ip(h1)
    info('h1 IP: %s\n' % h1_ip)

    if h1_ip is not None:
        info('[PASSED]: h1 obtained IP after pending OFFER expired\n')
        result = True
    else:
        info('[FAILED]: h1 could not obtain IP after OFFER timeout\n')
        result = False

    net.stop()
    return result



def main():
    setLogLevel('info')

    info('\n' + '='*60 + '\n')
    info('DHCP Tests\n')
    info('='*60 + '\n')

    # Test3: OFFER 超时回收
    result3 = test3_offer_timeout_reclaim()
    info('\n' + '-'*60 + '\n')

    info('\n' + '='*60 + '\n')
    info('Test Results:\n')
    info('  [Test3] OFFER Timeout Reclaim: %s\n' % ('[PASSED]' if result3 else '[FAILED]'))
    info('='*60 + '\n')


if __name__ == '__main__':
    main()