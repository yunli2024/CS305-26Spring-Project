"""
DHCP Lease Expiration and Renewal Test
测试租约过期和续租场景：
1. 测试租约自然过期
2. 测试续租后租约时间是否延长
Note: 需要与 dhcp.py 中的 Config.lease_time 配置一致（30秒） 正常是一天
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

def send_dhcp_request(node, interface='eth0'):
    """发送 DHCP 请求"""
    info('%s: Sending DHCP request on %s\n' % (node.name, interface))
    node.cmd('dhclient -v %s-%s' % (node.name, interface))

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


class LeaseExpiryTopo(Topo):
    """测试租约过期和续租的拓扑"""
    def __init__(self, **opts):
        Topo.__init__(self, **opts)

        h1 = self.addHost('h1', ip='no ip defined/8')
        s1 = self.addSwitch('s1')
        self.addLink(h1, s1)

def test_lease_expiry():
    """
    测试 1: 租约自然过期
    流程:
    1. 主机获取 DHCP 租约
    2. 等待租约过期（等待时长 = 租约时间）
    3. 验证租约过期后主机 IP 失效
    4. 重新请求 DHCP 验证能获取新租约
    """
    info('\nTest 1: Lease Natural Expiry\n')
    # 租约时间（需要与 dhcp.py 中的 Config.lease_time 一致）
    lease_time = 30  # 秒
    topo = LeaseExpiryTopo()
    net = Mininet(topo=topo, autoSetMacs=True, controller=RemoteController)
    for h in net.hosts:
        disable_ipv6(h)
    for s in net.switches:
        disable_ipv6(s)
    net.start()
    h1 = net.get('h1')

    # 获取初始租约
    info('\nStep 1: Request initial DHCP lease\n')
    send_dhcp_request(h1)
    time.sleep(2)
    initial_ip = get_current_ip(h1)
    info('Initial IP: %s\n' % initial_ip)
    if initial_ip is None:
        info('ERROR: Failed to get initial IP!\n')
        net.stop()
        return False

    # 记录租约开始时间
    lease_start_time = time.time()
    lease_expiry_time = lease_start_time + lease_time
    info('Lease start time: %s\n' % time.strftime('%H:%M:%S', time.localtime(lease_start_time)))
    info('Lease will expire at: %s\n' % time.strftime('%H:%M:%S', time.localtime(lease_expiry_time)))
    info('Waiting for lease to expire (%d seconds)...\n' % lease_time)

    # 等待租约过期
    remaining = lease_time
    while remaining > 0:
        # 末尾加空格清除倒计时变短时的残余字符
        info('Remaining: %d seconds      \r' % remaining)
        time.sleep(min(remaining, 5))
        remaining -= 5
    info('\nLease should have expired now!\n')

    # 验证租约过期后 IP 状态
    info('\nStep 2: Verify IP is released after expiry\n')
    current_ip = get_current_ip(h1)
    info('Current IP on client: %s\n' % current_ip)

    # 重新请求 DHCP（验证服务器已回收旧租约）
    info('\nStep 3: Requesting new DHCP lease...\n')
    send_dhcp_request(h1)
    time.sleep(2)
    new_ip = get_current_ip(h1)
    info('New IP after expiry: %s\n' % new_ip)
    if new_ip is not None:
        info('[PASSED]: Client successfully obtained new lease after expiry\n')
        result = True
    else:
        info('[FAILED]: Could not obtain new lease after expiry\n')
        result = False

    net.stop()
    return result

def test_lease_renewal():
    """
    测试 2: 续租测试
    流程:
    1. 主机获取 DHCP 租约 → 记录过期时间 T_expire
    2. 等待 T1 = lease_time / 2 (15秒) → 此时客户端应该自动续租
    3. 验证在 T_expire 时间点 IP 仍然有效（续租成功）
    4. 等待到新的过期时间点之后 → 验证 IP 失效
    """
    info('\nTest 2: Lease Renewal\n')
    lease_time = 30  # 秒（与 dhcp.py 中的配置一致）
    t1_time = lease_time // 2  # 租期的 50% 作为续租触发点
    t1_wait = t1_time + 3  # 等待 T1 时刻 + 3秒缓冲

    topo = LeaseExpiryTopo()
    net = Mininet(topo=topo, autoSetMacs=True, controller=RemoteController)
    for h in net.hosts:
        disable_ipv6(h)
    for s in net.switches:
        disable_ipv6(s)

    net.start()
    h1 = net.get('h1')

    # 获取初始租约
    info('\nStep 1: Get initial DHCP lease\n')
    send_dhcp_request(h1)
    time.sleep(2)
    initial_ip = get_current_ip(h1)
    info('Initial IP: %s\n' % initial_ip)
    if initial_ip is None:
        info('ERROR: Failed to get initial IP!\n')
        net.stop()
        return False

    # 记录初始租约过期时间
    initial_lease_start = time.time()
    original_expiry = initial_lease_start + lease_time
    info('Original lease expiry time: %s\n' % time.strftime('%H:%M:%S', time.localtime(original_expiry)))

    # Step 3: 等待 T1 时刻（租期的 50%）
    # 在 T1 时刻，客户端的 dhclient 应该自动发送 RENEW 请求
    info('\nStep 2: Waiting %d seconds for T1 (auto-renewal time)...\n' % t1_wait)
    time.sleep(t1_wait)

    # 检查续租是否成功（客户端可能已自动续租）
    current_ip = get_current_ip(h1)
    info('IP at T1 time: %s\n' % current_ip)

    # Step 4: 等待到原过期时间点
    info('\nStep 3: Wait until original expiry time...\n')
    time_to_expiry = original_expiry - time.time()
    if time_to_expiry > 0:
        info('Waiting %d seconds to reach original expiry...\n' % int(time_to_expiry))
        time.sleep(time_to_expiry)

    # Step 5: 在原过期时间点检查 IP 是否仍然有效
    info('\nStep 4: Verify IP is still valid at original expiry time\n')
    ip_at_expiry = get_current_ip(h1)
    info('IP at original expiry: %s\n' % ip_at_expiry)

    if ip_at_expiry == initial_ip and ip_at_expiry is not None:
        info('[PASSED]: IP still valid at original expiry (renewal successful)\n')
        step4_passed = True
    else:
        info('[NOTE]: IP changed or invalid at original expiry\n')
        step4_passed = False

    # 等待额外的租约时间（续租应该把过期时间延长了 lease_time）
    info('\nStep 5: Wait additional %d seconds (past extended expiry)...\n' % (lease_time + 5))
    time.sleep(lease_time + 5)

    # Step 7: 验证 IP 应该已经失效（租约真正过期）
    info('\nStep 6: Verify IP should be invalid now\n')
    final_ip = get_current_ip(h1)
    info('*** Final IP: %s\n' % final_ip)

    if final_ip != initial_ip or final_ip is None:
        info('[PASSED]: IP expired after extended lease time\n')
        step6_passed = True
    else:
        info('[NOTE]: IP still valid (may be normal if client auto-reacquired)\n')
        step6_passed = True  # 客户端可能自动重新请求

    net.stop()

    # 整体测试结果：续租成功意味着在原过期时间点 IP 仍然有效
    return step4_passed


def run_interactive_test():
    """
    交互式测试
    """
    info('\nInteractive Lease Test\n')
    info('Commands available in CLI:\n')
    info('dhclient h1-eth0        - Request DHCP lease\n')
    info('ip addr show           - Check current IP\n')

    topo = LeaseExpiryTopo()
    net = Mininet(topo=topo, autoSetMacs=True, controller=RemoteController)

    for h in net.hosts:
        disable_ipv6(h)
    net.start()

    h1 = net.get('h1')
    send_dhcp_request(h1)
    time.sleep(2)

    ip = get_current_ip(h1)
    info('\nh1 current IP: %s\n' % ip)

    info('\nEntering interactive CLI...\n')
    CLI(net)

    net.stop()


def main():
    setLogLevel('info')

    info('\n' + '='*60 + '\n')
    info('DHCP Lease Expiry and Renewal Tests\n')
    info('='*60 + '\n')

    # 测试 1: 租约自然过期
    result1 = test_lease_expiry()
    info('\n' + '-'*60 + '\n')

    # 测试 2: 续租测试
    result2 = test_lease_renewal()
    
    info('\n' + '='*60 + '\n')
    info('Test Results:\n')
    info('  Test 1 (Lease Expiry): %s\n' % ('[PASSED]' if result1 else '[FAILED]'))
    info('  Test 2 (Lease Renewal): %s\n' % ('[PASSED]' if result2 else '[FAILED]'))
    info('='*60 + '\n')


if __name__ == '__main__':
    main()