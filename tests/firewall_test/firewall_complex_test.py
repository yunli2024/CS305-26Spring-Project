import os
import re
import time

from mininet.cli import CLI
from mininet.log import setLogLevel
from mininet.net import Mininet
from mininet.node import RemoteController
from mininet.topo import Topo


def disable_ipv6(node):
    node.cmd("sysctl -w net.ipv6.conf.all.disable_ipv6=1")
    node.cmd("sysctl -w net.ipv6.conf.default.disable_ipv6=1")
    node.cmd("sysctl -w net.ipv6.conf.lo.disable_ipv6=1")


def send_arp(node, count=2):
    node.cmd("arping -c %s -A -I %s-eth0 %s" % (count, node.name, node.IP()))


def do_arp_all(net):
    for host in net.hosts:
        send_arp(host)


def curl(host, url):
    cmd = (
        "curl -sS --connect-timeout 2 -m 3 "
        "-o /dev/null -w 'HTTP_CODE=%%{http_code}\\n' "
        "%s 2>&1; echo EXIT=$?" % url
    )
    return host.cmd(cmd)


def ping_result(src, dst_ip, count=3):
    output = src.cmd("ping -c %s -W 1 %s" % (count, dst_ip))
    received = 0
    match = re.search(r"%s packets transmitted, (\d+) received" % count, output)
    if match:
        received = int(match.group(1))
    return received, output


def print_case(name, passed, output):
    status = "PASS" if passed else "FAIL"
    print("\n[%s] %s" % (status, name))
    print(output.strip())


class ComplexFirewallTopo(Topo):
    """
    Multi-switch topology for firewall robustness testing.

          h3
          |
    h1--s1--s2--h4
     |   | \\ |
     |   |  s3
     |   | / |
     |  s4--s5--h2
     |   |
     |   h5

    h1 and h2 are separated by several redundant paths. The current
    firewall_rule.json should still block only h1 -> h2 ICMP and h1 -> h2
    TCP/80, while other hosts, reverse traffic, and TCP/8080 stay reachable.
    """

    def __init__(self, **opts):
        Topo.__init__(self, **opts)

        h1 = self.addHost("h1", ip="192.168.117.2/24")
        h2 = self.addHost("h2", ip="192.168.117.3/24")
        h3 = self.addHost("h3", ip="192.168.117.4/24")
        h4 = self.addHost("h4", ip="192.168.117.5/24")
        h5 = self.addHost("h5", ip="192.168.117.6/24")

        s1 = self.addSwitch("s1")
        s2 = self.addSwitch("s2")
        s3 = self.addSwitch("s3")
        s4 = self.addSwitch("s4")
        s5 = self.addSwitch("s5")

        self.addLink(h1, s1)
        self.addLink(h2, s5)
        self.addLink(h3, s3)
        self.addLink(h4, s2)
        self.addLink(h5, s4)

        self.addLink(s1, s2)
        self.addLink(s2, s3)
        self.addLink(s3, s5)
        self.addLink(s5, s4)
        self.addLink(s4, s1)
        self.addLink(s1, s3)
        self.addLink(s2, s5)


def run_mininet():
    net = Mininet(
        topo=ComplexFirewallTopo(),
        autoSetMacs=True,
        controller=RemoteController,
    )

    for node in net.hosts + net.switches:
        disable_ipv6(node)

    net.start()
    time.sleep(2)

    for _ in range(3):
        do_arp_all(net)
        time.sleep(1)

    h1 = net.get("h1")
    h2 = net.get("h2")
    h3 = net.get("h3")
    h4 = net.get("h4")
    h5 = net.get("h5")

    h2.cmd('pkill -f "python3 -m http.server" || true')
    h2.cmd("python3 -m http.server 80 --bind 192.168.117.3 >/tmp/h2-http80.log 2>&1 &")
    h2.cmd("python3 -m http.server 8080 --bind 192.168.117.3 >/tmp/h2-http8080.log 2>&1 &")
    time.sleep(1)

    tests = []

    received, output = ping_result(h1, "192.168.117.3")
    tests.append(("h1 -> h2 ICMP is blocked across multi-hop paths", received == 0, output))

    received, output = ping_result(h1, "192.168.117.4")
    tests.append(("h1 -> h3 ICMP is allowed", received > 0, output))

    received, output = ping_result(h4, "192.168.117.3")
    tests.append(("h4 -> h2 ICMP is allowed because source IP is different", received > 0, output))

    received, output = ping_result(h2, "192.168.117.2")
    tests.append(("h2 -> h1 reverse ICMP is allowed because rule is directional", received > 0, output))

    received, output = ping_result(h5, "192.168.117.4")
    tests.append(("unrelated h5 -> h3 ICMP remains reachable", received > 0, output))

    output = curl(h1, "http://192.168.117.3:80/")
    tests.append(("h1 -> h2 TCP/80 is blocked", "HTTP_CODE=000" in output or "EXIT=28" in output, output))

    output = curl(h1, "http://192.168.117.3:8080/")
    tests.append(("h1 -> h2 TCP/8080 is allowed", "HTTP_CODE=200" in output and "EXIT=0" in output, output))

    print("\n===== Complex firewall test results =====")
    for name, passed, output in tests:
        print_case(name, passed, output)

    passed_count = sum(1 for _, passed, _ in tests if passed)
    print("\n===== Summary: %s/%s checks passed =====" % (passed_count, len(tests)))

    print("\n===== Firewall flow entries on switches =====")
    for switch in net.switches:
        print("\n--- %s ---" % switch.name)
        print(switch.cmd("ovs-ofctl -O OpenFlow10 dump-flows %s | grep 305f || true" % switch.name).strip())

    if os.environ.get("MININET_CLI") == "1":
        CLI(net)

    h2.cmd('pkill -f "python3 -m http.server" || true')
    net.stop()

    if passed_count != len(tests):
        raise SystemExit(1)


if __name__ == "__main__":
    setLogLevel("info")
    run_mininet()
