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


class SevenSwitchSevenHostTopo(Topo):
    def build(self):
        hosts = {
            "h1": "10.0.0.1/24",
            "h2": "10.0.0.2/24",
            "h3": "10.0.0.3/24",
            "h4": "10.0.0.4/24",
            "h5": "10.0.0.5/24",
            "h6": "10.0.0.6/24",
            "h7": "10.0.0.7/24",
        }
        for host, ip in hosts.items():
            self.addHost(host, ip=ip)

        for idx in range(1, 8):
            self.addSwitch("s%d" % idx)

        self.addLink("h1", "s1")
        self.addLink("h2", "s2")
        self.addLink("h3", "s3")
        self.addLink("h4", "s4")
        self.addLink("h5", "s5")
        self.addLink("h6", "s6")
        self.addLink("h7", "s4")

        switch_links = [
            ("s1", "s2"),
            ("s2", "s3"),
            ("s3", "s4"),
            ("s4", "s5"),
            ("s5", "s6"),
            ("s6", "s7"),
            ("s7", "s1"),
            ("s2", "s5"),
            ("s3", "s6"),
            ("s4", "s7"),
            ("s1", "s4"),
            ("s2", "s7"),
        ]
        for left, right in switch_links:
            self.addLink(left, right)


def print_demo_hint():
    print("\n===== Manual Demo Commands =====")
    print("Suggested order inside Mininet CLI:")
    print("  pingall")
    print("  net")
    print("  link s2 s5 down")
    print("  pingall")
    print("  link s2 s5 up")
    print("  pingall")
    print("  switch s6 stop")
    print("  switch s6 start")
    print("  pingall")
    print("  sh ovs-ofctl mod-port s4 4 down")
    print("  pingall")
    print("  sh ovs-ofctl mod-port s4 4 up")
    print("  pingall")
    print("  arping_all")
    print("  exit")


def run_mininet():
    net = Mininet(
        topo=SevenSwitchSevenHostTopo(),
        autoSetMacs=True,
        controller=RemoteController,
    )

    for node in net.hosts + net.switches:
        disable_ipv6(node)

    net.start()
    time.sleep(3)

    print("\n===== Warm Up Host Discovery =====")
    do_arp_all(net)
    time.sleep(2)
    do_arp_all(net)
    time.sleep(1)

    print_demo_hint()
    CLI(net)
    net.stop()


if __name__ == "__main__":
    setLogLevel("info")
    run_mininet()
