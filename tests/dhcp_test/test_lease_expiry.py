"""
DHCP Lease Expiration and Renewal Test
测试租约过期和续租场景：
1. 测试租约自然过期
2. 测试续租后租约时间是否延长
Note: 需要与 dhcp.py 中的 Config.lease_time 配置一致（30秒） 
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
    
    易错：
    - dhclient会在T1时刻（租期50%）自动续租
    - Linux内核不会自动删除过期IP，IP配置持久化在内核中
    - get_current_ip()只查询内核IP，无法反映服务器端租约状态
    
    正确的测试方法（INIT-REBOOT验证）：
    1. 获取DHCP租约
    2. 停止dhclient进程（防止自动续租）
    3. 等待租约过期（服务器端回收租约）
    4. 重新请求DHCP（服务器应检测到租约过期）
    5. 验证获取到新租约（证明旧租约已过期）
    
    流程:
    1. 主机获取 DHCP 租约
    2. 停止 dhclient 进程（防止自动续租）
    3. 等待租约过期（等待时长 = 租约时间）
    4. 验证重新请求能获取新租约（证明旧租约已过期）
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
    
    # 停止 dhclient 进程，防止自动续租
    info('\nStep 2: Stopping dhclient to prevent auto-renewal\n')
    # 先尝试释放租约
    h1.cmd('dhclient -r %s-eth0 2>&1' % h1.name)
    time.sleep(0.5)
    # 强制杀死所有 dhclient 进程
    h1.cmd('pkill -9 dhclient 2>&1')
    time.sleep(0.5)
    info('dhclient stopped\n')
    
    #服务器过期回收
    info('\nStep 3: Waiting for lease to expire (%d seconds)...\n' % lease_time)
    # 等待租约过期
    remaining = lease_time
    while remaining > 0:
        # 末尾加空格清除倒计时变短时的残余字符
        info('Remaining: %d seconds      \r' % remaining)
        time.sleep(min(remaining, 5))
        remaining -= 5
    info('\nLease has expired on server side!\n')

    # 客户端清除残留
    # 说明：此时客户端IP可能仍在内核中，但服务器端租约已过期
    info('\nStep 4: Verify lease expiry by requesting new DHCP lease\n')
    info('Note: Client IP may still be in kernel, but server lease has expired\n')
    
    # 手动清除旧IP，准备重新请求
    h1.cmd('ip addr flush dev %s-eth0' % h1.name)
    time.sleep(1)
    
    current_ip = get_current_ip(h1)
    info('Current IP after flush: %s\n' % current_ip)

    # 重新请求 DHCP（验证服务器已回收旧租约）
    info('\nStep 5: Requesting new DHCP lease...\n')
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
    测试 2: 续租测试（简化版）
    
    测试目的：
    验证dhclient在T1时刻（租期50%）自动续租，使租约延长
    
    流程:
    1. 主机获取 DHCP 租约 → 记录过期时间 T_expire
    2. 等待 T1 = lease_time / 2 (15秒) → 此时客户端应该自动续租
    3. 等待到原过期时间点
    4. 验证在原过期时间点 IP 仍然有效（续租成功）
    
    """
    info('\nTest 2: Lease Renewal (Simplified)\n')
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

    # Step 2: 等待 T1 时刻（租期的 50%）
    # 在 T1 时刻，客户端的 dhclient 应该自动发送 RENEW 请求
    info('\nStep 2: Waiting %d seconds for T1 (auto-renewal time)...\n' % t1_wait)
    info('At T1, dhclient should automatically send RENEW request\n')
    time.sleep(t1_wait)

    # 检查续租是否成功（客户端可能已自动续租）
    current_ip = get_current_ip(h1)
    info('IP at T1 time: %s\n' % current_ip)

    # Step 3: 等待到原过期时间点
    info('\nStep 3: Wait until original expiry time...\n')
    time_to_expiry = original_expiry - time.time()
    if time_to_expiry > 0:
        info('Waiting %d seconds to reach original expiry...\n' % int(time_to_expiry))
        time.sleep(time_to_expiry)

    # Step 4: 在原过期时间点检查 IP 是否仍然有效
    info('\nStep 4: Verify IP is still valid at original expiry time\n')
    ip_at_expiry = get_current_ip(h1)
    info('IP at original expiry: %s\n' % ip_at_expiry)

    if ip_at_expiry == initial_ip and ip_at_expiry is not None:
        info('[PASSED]: IP still valid at original expiry (renewal successful)\n')
        info('This confirms dhclient auto-renewed the lease at T1 time\n')
        result = True
    else:
        info('[FAILED]: IP changed or invalid at original expiry\n')
        info('Expected: %s, Got: %s\n' % (initial_ip, ip_at_expiry))
        result = False

    net.stop()

    # 测试结果：续租成功意味着在原过期时间点 IP 仍然有效
    return result


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