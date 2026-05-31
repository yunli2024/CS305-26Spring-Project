import argparse
import re
import sys
import time

from mininet.link import TCLink
from mininet.log import setLogLevel
from mininet.net import Mininet
from mininet.node import OVSBridge
from mininet.topo import Topo


class TcpCongestionTopo(Topo):
    # 创建拓扑
    def __init__(self, loss, **opts):
        Topo.__init__(self, **opts)

        # 初始化主机和交换机
        h1 = self.addHost("h1", ip="10.0.0.1/24")
        h2 = self.addHost("h2", ip="10.0.0.2/24")
        h3 = self.addHost("h3", ip="10.0.0.3/24")
        h4 = self.addHost("h4", ip="10.0.0.4/24")
        s1 = self.addSwitch("s1")
        s2 = self.addSwitch("s2")

        # 主机接入链路设置得比较快，避免它们成为瓶颈。
        # bandwidth 100MBps delay 1ms
        for host, switch in [(h1, s1), (h3, s1), (h2, s2), (h4, s2)]:
            self.addLink(host, switch, bw=100, delay="1ms", use_htb=True)

        # s1-s2的链路是瓶颈链路 带宽较小，延迟较低，丢包率可配置
        self.addLink(
            s1,
            s2,
            bw=10,
            delay="30ms",
            loss=loss,
            max_queue_size=100,
            use_htb=True,
        )


def disable_ipv6(node):
    node.cmd("sysctl -w net.ipv6.conf.all.disable_ipv6=1 >/dev/null 2>&1")
    node.cmd("sysctl -w net.ipv6.conf.default.disable_ipv6=1 >/dev/null 2>&1")
    node.cmd("sysctl -w net.ipv6.conf.lo.disable_ipv6=1 >/dev/null 2>&1")


def parse_iperf(output):
    # 从 iperf 输出中提取 TCP 吞吐量，统一返回 Mbits/sec。
    matches = re.findall(r"(\d+(?:\.\d+)?)\s+([KMG]?)bits/sec", output)
    if not matches:
        return None
    value, unit = matches[-1]
    throughput = float(value)
    if unit == "K":
        throughput /= 1000
    elif unit == "G":
        throughput *= 1000
    return throughput


def available_algorithms(host):
    # 使用命令行，查看有什么可以使用的拥塞控制方式
    output = host.cmd("sysctl -n net.ipv4.tcp_available_congestion_control")
    return output.split()


def set_tcp_algorithm(host, algorithm):
    # 设置拥塞控制的算法
    # 如果系统不支持某个算法，就跳过这一组实验。
    if algorithm not in available_algorithms(host):
        print("[skip] %s does not support %s" % (host.name, algorithm))
        return False

    host.cmd("sysctl -w net.ipv4.tcp_congestion_control=%s >/dev/null" % algorithm)
    return True


def jain_fairness(x1, x2):
    # Jain fairness index，越接近 1 表示两条流越公平。
    if x1 is None or x2 is None:
        return None
    denominator = 2 * (x1 ** 2 + x2 ** 2)
    if denominator == 0:
        return None
    return (x1 + x2) ** 2 / denominator


def run_single_case(algorithm, loss, duration):
    # 单流实验：h1 -> h2，用来比较 Reno 和 CUBIC 的吞吐量。
    # 构建网络，将loss设置到瓶颈链路s1-s2上
    net = Mininet(
        topo=TcpCongestionTopo(loss),
        link=TCLink,
        switch=OVSBridge,
        controller=None,
        autoSetMacs=True,
    )

    try:
        net.start()
        for node in net.hosts + net.switches:
            disable_ipv6(node)
        time.sleep(1)

        # 取出发送端和接收端，这里只用到了h1作为client h2作为server
        h1, h2 = net.get("h1"), net.get("h2")
        print("\n===== Single flow: %s, loss=%s%% =====" % (algorithm, loss))

        # 设置h1的TCP拥塞控制算法为传入的algorithm
        # 如果不支持就跳过
        if not set_tcp_algorithm(h1, algorithm):
            return None
        

        # 先杀死旧的h2的iperf进程
        # 然后在h2上启动新的iperf server监听port 5001
        h2.cmd("pkill -f 'iperf -s' || true")
        h2.cmd("iperf -s -p 5001 >/tmp/bonus5_tcp_single_server.log 2>&1 &")
        time.sleep(1)


        # 在h1上启动iperf client
        # h1 向 h2 发送TCP流量，持续duration秒，得到结果存在output中
        output = h1.cmd("iperf -c %s -p 5001 -t %s" % (h2.IP(), duration))
        return {
            "algorithm": algorithm,
            "loss": loss,
            "throughput": parse_iperf(output), # 解析output中的iperf吞吐量信息，并返回
        }
    finally:
        net.stop()


def run_fairness_case(loss, duration):
    # 双流实验：h1->h2 使用 Reno，h3->h4 使用 CUBIC，观察两者共享瓶颈时是否公平。
    net = Mininet(
        topo=TcpCongestionTopo(loss),
        link=TCLink,
        switch=OVSBridge,
        controller=None,
        autoSetMacs=True,
    )

    try:
        net.start()
        for node in net.hosts + net.switches:
            disable_ipv6(node)
        time.sleep(1)

        h1, h2, h3, h4 = [net.get(name) for name in ("h1", "h2", "h3", "h4")]
        print("\n===== Fairness: reno vs cubic, loss=%s%% =====" % loss)

        if not set_tcp_algorithm(h1, "reno"):
            return None
        if not set_tcp_algorithm(h3, "cubic"):
            return None

        h2.cmd("pkill -f 'iperf -s' || true")
        h4.cmd("pkill -f 'iperf -s' || true")
        h2.cmd("iperf -s -p 5001 >/tmp/bonus5_tcp_server1.log 2>&1 &")
        h4.cmd("iperf -s -p 5002 >/tmp/bonus5_tcp_server2.log 2>&1 &")
        time.sleep(1)

        h1.cmd(
            "iperf -c %s -p 5001 -t %s >/tmp/bonus5_tcp_flow1.log 2>&1 &"
            % (h2.IP(), duration)
        )
        h3.cmd(
            "iperf -c %s -p 5002 -t %s >/tmp/bonus5_tcp_flow2.log 2>&1 &"
            % (h4.IP(), duration)
        )
        time.sleep(duration + 1)

        flow1 = parse_iperf(h1.cmd("cat /tmp/bonus5_tcp_flow1.log"))
        flow2 = parse_iperf(h3.cmd("cat /tmp/bonus5_tcp_flow2.log"))

        return {
            "reno": flow1,
            "cubic": flow2,
            "fairness": jain_fairness(flow1, flow2),
        }
    finally:
        net.stop()


def print_summary(single_results, fairness_result):
    print("\n===== Single Flow Summary =====")
    print("algorithm\tloss(%)\tthroughput(Mbps)")
    for item in single_results:
        if item:
            print("%s\t\t%s\t%s" % (item["algorithm"], item["loss"], item["throughput"]))

    print("\n===== Fairness Summary =====")
    print("reno(Mbps)\tcubic(Mbps)\tfairness")
    if fairness_result:
        print(
            "%s\t\t%s\t\t%s"
            % (
                fairness_result["reno"],
                fairness_result["cubic"],
                fairness_result["fairness"],
            )
        )


def main():
    parser = argparse.ArgumentParser(description="Bonus5 TCP congestion control test")
    parser.add_argument("--loss", type=float, default=0.5, help="bottleneck link loss rate")
    parser.add_argument("--duration", type=int, default=15, help="iperf duration")
    args = parser.parse_args()

    single_results = [
        run_single_case("reno", args.loss, args.duration),
        run_single_case("cubic", args.loss, args.duration),
    ]
    fairness_result = run_fairness_case(args.loss, args.duration)
    print_summary(single_results, fairness_result)


if __name__ == "__main__":
    setLogLevel("info")
    sys.exit(main())
