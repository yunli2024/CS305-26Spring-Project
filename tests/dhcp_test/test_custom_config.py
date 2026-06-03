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


def send_dhcp_request(node, interface="eth0"):
    info("%s: Sending DHCP request on %s\n" % (node.name, interface))
    node.cmd("dhclient -v %s-%s" % (node.name, interface))


def get_current_ip(node, interface="eth0"):
    output = node.cmd("ip addr show %s-%s" % (node.name, interface))
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("inet "):
            return line.split()[1].split("/")[0]
    return None


class CustomConfigTopo(Topo):
    def build(self):
        s1 = self.addSwitch("s1")
        for idx in range(1, 4):
            host = self.addHost("h%d" % idx, ip="no ip defined/8")
            self.addLink(host, s1)


def main():
    setLogLevel("info")

    start_ip = os.environ.get("DHCP_START_IP", "10.10.0.10")
    end_ip = os.environ.get("DHCP_END_IP", "10.10.0.12")
    netmask = os.environ.get("DHCP_NETMASK", "255.255.255.248")
    network = ipaddress.IPv4Network("%s/%s" % (start_ip, netmask), strict=False)
    start_addr = ipaddress.IPv4Address(start_ip)
    end_addr = ipaddress.IPv4Address(end_ip)

    info("\n===== DHCP Custom Config Test =====\n")
    info("Expected pool: %s - %s, netmask=%s\n" % (start_ip, end_ip, netmask))
    info("Expected subnet: %s\n" % network)

    net = Mininet(topo=CustomConfigTopo(), autoSetMacs=True, controller=RemoteController)
    for node in net.hosts + net.switches:
        disable_ipv6(node)

    net.start()

    assigned = {}
    for host in net.hosts:
        send_dhcp_request(host)
        time.sleep(1)
        assigned[host.name] = get_current_ip(host)

    info("\n===== Assigned Addresses =====\n")
    all_ok = True
    for host_name, ip in assigned.items():
        info("%s -> %s\n" % (host_name, ip))
        if ip is None:
            all_ok = False
            continue
        addr = ipaddress.IPv4Address(ip)
        if addr < start_addr or addr > end_addr:
            info("[FAIL] %s is outside configured pool\n" % ip)
            all_ok = False
        if addr not in network:
            info("[FAIL] %s is outside configured subnet\n" % ip)
            all_ok = False

    unique_ok = len(set(ip for ip in assigned.values() if ip is not None)) == len(assigned)
    if not unique_ok:
        info("[FAIL] Duplicate DHCP allocation detected\n")
        all_ok = False

    if assigned.get("h1") and assigned.get("h2"):
        output = net.get("h1").cmd("ping -c 2 -W 1 %s" % assigned["h2"])
        info("\n===== Connectivity Check h1 -> h2 =====\n")
        info(output)
        if "0% packet loss" not in output:
            all_ok = False

    net.stop()

    if all_ok:
        info("\n[PASS] DHCP custom configuration test passed\n")
        return
    raise SystemExit(1)


if __name__ == "__main__":
    main()
