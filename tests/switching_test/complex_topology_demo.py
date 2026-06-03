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


def print_step(title):
    print("\n===== %s =====" % title)


def port_between(src_switch, dst_switch):
    connections = src_switch.connectionsTo(dst_switch)
    if not connections:
        raise RuntimeError("No direct link between %s and %s" % (src_switch.name, dst_switch.name))
    src_intf, _ = connections[0]
    return src_switch.ports[src_intf]


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


def show_demo_commands():
    print_step("Suggested CLI Demo Commands")
    print("pingall")
    print("net")
    print("switch s6 stop")
    print("switch s6 start")
    print("link s2 s5 down")
    print("link s2 s5 up")
    print("sh ovs-ofctl mod-port s4 4 down")
    print("sh ovs-ofctl mod-port s4 4 up")
    print("arping_all")


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
    do_arp_all(net)
    time.sleep(2)

    print_step("Warm Up")
    net.pingAll()
    time.sleep(2)
    do_arp_all(net)
    time.sleep(1)

    print_step("Initial Reachability")
    initial_loss = net.pingAll()

    print_step("Link Failure: s2-s5 down")
    net.configLinkStatus("s2", "s5", "down")
    time.sleep(3)
    do_arp_all(net)
    time.sleep(2)
    link_down_loss = net.pingAll()

    print_step("Link Recovery: s2-s5 up")
    net.configLinkStatus("s2", "s5", "up")
    time.sleep(3)
    do_arp_all(net)
    time.sleep(2)

    print_step("Switch Failure: stop s6")
    s6 = net.get("s6")
    s6.stop(deleteIntfs=False)
    time.sleep(4)
    do_arp_all(net)
    time.sleep(2)

    print_step("Switch Recovery: start s6")
    s6.start(net.controllers)
    time.sleep(5)
    do_arp_all(net)
    time.sleep(2)
    switch_recovery_loss = net.pingAll()

    print_step("Port Modify: disable s4->s7 port")
    s4 = net.get("s4")
    s7 = net.get("s7")
    s4_port_to_s7 = port_between(s4, s7)
    s4.cmd("ovs-ofctl -O OpenFlow10 mod-port %s %s down" % (s4.name, s4_port_to_s7))
    time.sleep(4)
    do_arp_all(net)
    time.sleep(2)
    port_down_loss = net.pingAll()

    print_step("Port Restore: enable s4->s7 port")
    s4.cmd("ovs-ofctl -O OpenFlow10 mod-port %s %s up" % (s4.name, s4_port_to_s7))
    time.sleep(4)
    do_arp_all(net)
    time.sleep(2)
    final_loss = net.pingAll()

    show_demo_commands()

    if os.environ.get("MININET_CLI") == "1":
        CLI(net)

    net.stop()

    if any(loss != 0 for loss in [initial_loss, link_down_loss, switch_recovery_loss, port_down_loss, final_loss]):
        raise SystemExit(1)


if __name__ == "__main__":
    setLogLevel("info")
    run_mininet()
