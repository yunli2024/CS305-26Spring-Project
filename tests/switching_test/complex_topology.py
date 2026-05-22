import os
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


class ComplexSwitchingTopo(Topo):
    def __init__(self, **opts):
        Topo.__init__(self, **opts)

        h1 = self.addHost("h1")
        h2 = self.addHost("h2")
        h3 = self.addHost("h3")
        h4 = self.addHost("h4")

        s1 = self.addSwitch("s1")
        s2 = self.addSwitch("s2")
        s3 = self.addSwitch("s3")
        s4 = self.addSwitch("s4")
        s5 = self.addSwitch("s5")

        self.addLink(h1, s1)
        self.addLink(h2, s3)
        self.addLink(h3, s4)
        self.addLink(h4, s5)

        self.addLink(s1, s2)
        self.addLink(s2, s3)
        self.addLink(s3, s5)
        self.addLink(s5, s4)
        self.addLink(s4, s1)
        self.addLink(s2, s5)
        self.addLink(s1, s3)


def run_mininet():
    net = Mininet(
        topo=ComplexSwitchingTopo(),
        autoSetMacs=True,
        controller=RemoteController,
    )

    for node in net.hosts + net.switches:
        disable_ipv6(node)

    net.start()
    time.sleep(2)
    do_arp_all(net)
    time.sleep(2)

    print("\n===== Warm up controller flows =====")
    net.pingAll()
    time.sleep(1)
    do_arp_all(net)
    time.sleep(1)

    print("\n===== Initial complex topology pingAll =====")
    initial_loss = net.pingAll()

    print("\n===== Bring s1-s3 down and test fallback paths =====")
    net.configLinkStatus("s1", "s3", "down")
    time.sleep(3)
    do_arp_all(net)
    time.sleep(2)
    fallback_loss = net.pingAll()

    print("\n===== Restore s1-s3 =====")
    net.configLinkStatus("s1", "s3", "up")
    time.sleep(3)
    do_arp_all(net)

    if os.environ.get("MININET_CLI") == "1":
        CLI(net)

    net.stop()
    return initial_loss, fallback_loss


if __name__ == "__main__":
    setLogLevel("info")
    run_mininet()
