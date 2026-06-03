import ipaddress
import os
import time

from mininet.log import info, setLogLevel
from mininet.net import Mininet
from mininet.node import RemoteController
from mininet.topo import Topo


def disable_ipv6(node):
    node.cmd("sysctl -w net.ipv6.conf.all.disable_ipv6=1")
    node.cmd("sysctl -w net.ipv6.conf.default.disable_ipv6=1")
    node.cmd("sysctl -w net.ipv6.conf.lo.disable_ipv6=1")


def send_dhcp_request_async(node, interface="eth0"):
    info("%s: Sending DHCP request on %s\n" % (node.name, interface))
    node.cmd("dhclient -v -nw %s-%s >/tmp/%s-dhclient.log 2>&1 &" % (node.name, interface, node.name))


def get_current_ip(node, interface="eth0"):
    output = node.cmd("ip addr show %s-%s" % (node.name, interface))
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("inet "):
            return line.split()[1].split("/")[0]
    return None


class SmallPoolTopo(Topo):
    def build(self):
        s1 = self.addSwitch("s1")
        for idx in range(1, 6):
            host = self.addHost("h%d" % idx, ip="no ip defined/8")
            self.addLink(host, s1)


def main():
    setLogLevel("info")

    start_ip = os.environ.get("DHCP_START_IP", "10.10.0.10")
    end_ip = os.environ.get("DHCP_END_IP", "10.10.0.12")
    start_addr = ipaddress.IPv4Address(start_ip)
    end_addr = ipaddress.IPv4Address(end_ip)
    pool_size = int(end_addr) - int(start_addr) + 1

    info("\n===== DHCP Pool Exhaustion Demo =====\n")
    info("Pool size=%d, host count=5\n" % pool_size)

    net = Mininet(topo=SmallPoolTopo(), autoSetMacs=True, controller=RemoteController)
    for node in net.hosts + net.switches:
        disable_ipv6(node)
    net.start()

    for host in net.hosts:
        send_dhcp_request_async(host)

    time.sleep(8)

    assigned = {}
    for host in net.hosts:
        assigned[host.name] = get_current_ip(host)

    info("\n===== Allocation Results =====\n")
    for host_name, ip in assigned.items():
        info("%s -> %s\n" % (host_name, ip))

    success_hosts = [host for host, ip in assigned.items() if ip is not None]
    failed_hosts = [host for host, ip in assigned.items() if ip is None]
    in_range = all(
        start_addr <= ipaddress.IPv4Address(ip) <= end_addr
        for ip in assigned.values()
        if ip is not None
    )
    unique_ok = len({ip for ip in assigned.values() if ip is not None}) == len(success_hosts)

    info("\nExpected: first %d hosts receive IPs, remaining hosts do not.\n" % pool_size)
    info("Allocated hosts: %s\n" % ", ".join(success_hosts))
    info("Unallocated hosts: %s\n" % ", ".join(failed_hosts))

    net.stop()

    if len(success_hosts) == pool_size and len(failed_hosts) == len(assigned) - pool_size and in_range and unique_ok:
        info("\n[PASS] DHCP pool exhaustion test passed\n")
        return
    raise SystemExit(1)


if __name__ == "__main__":
    main()
