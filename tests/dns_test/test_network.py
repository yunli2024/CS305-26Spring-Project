import time

from mininet.log import setLogLevel
from mininet.net import Mininet
from mininet.node import RemoteController
from mininet.topo import Topo

from dns_query_command import build_python_script_command


def disable_ipv6(node):
    node.cmd("sysctl -w net.ipv6.conf.all.disable_ipv6=1")
    node.cmd("sysctl -w net.ipv6.conf.default.disable_ipv6=1")
    node.cmd("sysctl -w net.ipv6.conf.lo.disable_ipv6=1")


def send_arp(node, count=2):
    node.cmd("arping -c %s -A -I %s-eth0 %s" % (count, node.name, node.IP()))


def do_arp_all(net):
    for host in net.hosts:
        send_arp(host)


DNS_QUERY_SCRIPT = r"""
import socket
import struct
import sys

def encode_qname(name):
    payload = b""
    for part in name.split("."):
        payload += bytes([len(part)]) + part.encode("ascii")
    return payload + b"\x00"

name = sys.argv[1]
expected = sys.argv[2]
query_id = 0x3054
header = struct.pack("!HHHHHH", query_id, 0x0100, 1, 0, 0, 0)
question = encode_qname(name) + struct.pack("!HH", 1, 1)

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.settimeout(3)
sock.sendto(header + question, ("192.168.1.1", 53))
data, addr = sock.recvfrom(512)

flags = struct.unpack("!H", data[2:4])[0]
answers = struct.unpack("!H", data[6:8])[0]
print("DNS_FLAGS=%s ANSWERS=%s FROM=%s" % (hex(flags), answers, addr[0]))

if expected == "NXDOMAIN":
    if flags & 0x000f == 3 and answers == 0:
        print("DNS_TEST_PASS")
        raise SystemExit(0)
    print("DNS_TEST_FAIL")
    raise SystemExit(1)

if socket.inet_aton(expected) in data and answers == 1:
    print("DNS_TEST_PASS")
    raise SystemExit(0)

print("DNS_TEST_FAIL")
raise SystemExit(1)
"""


class DNSTopo(Topo):
    def __init__(self, **opts):
        Topo.__init__(self, **opts)
        h1 = self.addHost("h1", ip="192.168.1.2/24")
        h2 = self.addHost("h2", ip="192.168.1.3/24")
        s1 = self.addSwitch("s1")
        self.addLink(h1, s1)
        self.addLink(h2, s1)


def run_dns_query(host, name, expected):
    command = build_python_script_command(DNS_QUERY_SCRIPT, [name, expected])
    command = "%s; echo EXIT=$?" % command
    return host.cmd(command)


def print_case(name, passed, output):
    status = "PASS" if passed else "FAIL"
    print("\n[%s] %s" % (status, name))
    print(output.strip())


def run_mininet():
    net = Mininet(
        topo=DNSTopo(),
        autoSetMacs=True,
        controller=RemoteController,
    )

    for node in net.hosts + net.switches:
        disable_ipv6(node)

    net.start()
    time.sleep(2)
    do_arp_all(net)
    time.sleep(2)

    h1 = net.get("h1")

    tests = []

    output = run_dns_query(h1, "web.cs305.local", "192.168.1.3")
    tests.append(("web.cs305.local resolves to 192.168.1.3", "DNS_TEST_PASS" in output and "EXIT=0" in output, output))

    output = run_dns_query(h1, "h1.cs305.local", "192.168.1.2")
    tests.append(("h1.cs305.local resolves to 192.168.1.2", "DNS_TEST_PASS" in output and "EXIT=0" in output, output))

    output = run_dns_query(h1, "missing.cs305.local", "NXDOMAIN")
    tests.append(("missing.cs305.local returns NXDOMAIN", "DNS_TEST_PASS" in output and "EXIT=0" in output, output))

    loss = net.pingAll()
    tests.append(("normal shortest-path forwarding still works", loss == 0, "packet loss=%s%%" % loss))

    print("\n===== DNS bonus test results =====")
    for name, passed, output in tests:
        print_case(name, passed, output)

    print("\n===== DNS PacketIn flow on s1 =====")
    s1 = net.get("s1")
    print(s1.cmd("ovs-ofctl -O OpenFlow10 dump-flows s1 | grep tp_dst=53 || true").strip())

    net.stop()

    passed_count = sum(1 for _, passed, _ in tests)
    print("\n===== Summary: %s/%s checks passed =====" % (passed_count, len(tests)))
    if passed_count != len(tests):
        raise SystemExit(1)


if __name__ == "__main__":
    setLogLevel("info")
    run_mininet()
