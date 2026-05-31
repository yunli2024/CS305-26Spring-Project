import argparse
import re
import sys
import time

from mininet.link import TCLink
from mininet.log import setLogLevel
from mininet.net import Mininet
from mininet.node import OVSBridge
from mininet.topo import Topo

class BufferbloatTopo(Topo):

    def __init__(self, queue_size, **opts):
        Topo.__init__(self, **opts)

        # 初始化host与switch
        h1 = self.addHost("h1", ip="10.0.0.1/24")
        h2 = self.addHost("h2", ip="10.0.0.2/24")
        h3 = self.addHost("h3", ip="10.0.0.3/24")
        h4 = self.addHost("h4", ip="10.0.0.4/24")
        s1 = self.addSwitch("s1")
        s2 = self.addSwitch("s2")

        # 配置主机与交换机之间的link的参数
        # 主机到交换机的接入口设置为 100 Mbps、1 ms。
        # 它们比中间链路快很多，所以不会成为实验中的瓶颈。
        for host, switch in [(h1, s1), (h2, s1), (h3, s2), (h4, s2)]:
            self.addLink(host, switch, bw=100, delay="1ms", use_htb=True)

        # s1-s2 作为瓶颈链路：
        # bw=1 表示带宽只有 1 Mbps；
        # delay="20ms" 表示链路本身有 20 ms 传播延迟；
        # max_queue_size 是可配置的参数，用来控制队列最多能缓存多少包。也就是这个实验中的Bufferbloat
        self.addLink(
            s1,
            s2,
            bw=1,
            delay="20ms",
            max_queue_size=queue_size,
            use_htb=True,
        )


def disable_ipv6(node):
    node.cmd("sysctl -w net.ipv6.conf.all.disable_ipv6=1 >/dev/null 2>&1")
    node.cmd("sysctl -w net.ipv6.conf.default.disable_ipv6=1 >/dev/null 2>&1")
    node.cmd("sysctl -w net.ipv6.conf.lo.disable_ipv6=1 >/dev/null 2>&1")


def parse_ping(output):
    """
    从 ping 的输出结果中提取两个指标。loss和avg_rtt

    ping 结尾通常会有类似这样的内容：
        0% packet loss
        rtt min/avg/max/mdev = 41.2/55.6/90.1/10.0 ms
    """
    loss_match = re.search(r"(\d+(?:\.\d+)?)% packet loss", output)
    rtt_match = re.search(
        r"(?:rtt|round-trip) min/avg/max/(?:mdev|stddev) = "
        r"[\d.]+/([\d.]+)/[\d.]+/[\d.]+",
        output,
    )
    loss = float(loss_match.group(1)) if loss_match else None
    avg_rtt = float(rtt_match.group(1)) if rtt_match else None
    return loss, avg_rtt


def parse_iperf(output):
    """
    从 iperf 输出中提取 TCP 吞吐量。

    iperf 输出里可能会出现多行 Mbits/sec。
    最后一行是汇总结果，所以这里取最后一个匹配值。
    为了方便比较，函数统一返回 Mbits/sec：
    """
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


def ping(host, dst_ip, count=8):
    """
    使用命令行让某个主机 ping 目标 IP。
    - count 控制发送多少个 ICMP 包；
    - -i 0.2 表示每 0.2 秒发一个包；
    - -W 2 表示每个包最多等待 2 秒。
    """
    return host.cmd("ping -c %s -i 0.2 -W 2 %s" % (count, dst_ip))


def run_case(queue_size, duration):
    """
    运行一次指定queue_size的实验。
    queue_size：瓶颈链路 s1-s2 的队列大小。
    duration：iperf TCP 大流量持续时间。
    """

    # 启动mininet网络，使用指定的size作为瓶颈链路s1-s2的buffer大小
    net = Mininet(
        topo=BufferbloatTopo(queue_size),
        link=TCLink,
        switch=OVSBridge,
        controller=None,
        autoSetMacs=True,
    )

    try:
        net.start()
        for node in net.hosts + net.switches:
            disable_ipv6(node)

        # 给交换机和接口一点初始化时间，避免刚启动时测到不稳定结果。
        time.sleep(1)

        h1, h2, h3, h4 = [net.get(name) for name in ("h1", "h2", "h3", "h4")]
        print("\n===== Queue size: %s packets =====" % queue_size)

        # 第一次 ping：网络空闲状态，没有大流量下，h2 ping h4的RTT。
        # 这个值作为 baseline，说明链路本身的基础 RTT 大概是多少。
        idle_ping = ping(h2, h4.IP())
        idle_loss, idle_rtt = parse_ping(idle_ping)

        # 启动 iperf server。
        h3.cmd("pkill -f 'iperf -s' || true")  # pkill避免上一次实验留下旧的 iperf server。
        h3.cmd("iperf -s -p 5001 >/tmp/bonus5_iperf_server.log 2>&1 &")
        time.sleep(1)

        # 先启动后台 ping，1 秒后再启动 iperf client。
        # ping 会覆盖 iperf 的大部分运行时间，用来观察拥塞时的小包延迟。
        h2.cmd("ping -c 30 -i 0.5 -W 2 %s >/tmp/bonus5_ping.log 2>&1 &" % h4.IP())
        time.sleep(1)
        # 前台启动 TCP 大流量。
        # 这里会阻塞 duration 秒，期间 h2 的 ping 正在后台持续运行。
        iperf_output = h1.cmd("iperf -c %s -p 5001 -t %s" % (h3.IP(), duration))
        time.sleep(1)

        # 读取后台 ping 的完整输出。再进行相应变量的解析。
        busy_ping = h2.cmd("cat /tmp/bonus5_ping.log")

        busy_loss, busy_rtt = parse_ping(busy_ping)
        throughput = parse_iperf(iperf_output)

        return {
            "queue": queue_size,
            "idle_loss": idle_loss,
            "idle_rtt": idle_rtt,
            "busy_loss": busy_loss,
            "busy_rtt": busy_rtt,
            "throughput": throughput,
        }
    finally:
        # 不管实验是否成功，都停止 Mininet，避免影响下一次运行。
        net.stop()


def print_summary(results):
    print("\n===== Summary =====")
    print("queue\tthroughput(Mbps)\tidle_rtt(ms)\tbusy_rtt(ms)\tloss(%)")
    for item in results:
        print(
            "%s\t%s\t\t%s\t\t%s\t\t%s"
            % (
                item["queue"],
                item["throughput"],
                item["idle_rtt"],
                item["busy_rtt"],
                item["busy_loss"],
            )
        )


def main():
    # 进行测试，默认的小队列大小20packets，大队列大小1000packets，duration15s
    parser = argparse.ArgumentParser(description="Bonus5 Bufferbloat Mininet test")
    parser.add_argument("--queue", type=int, help="only test 1 queue size, for example 20 or 1000")
    parser.add_argument("--duration", type=int, default=15, help="iperf duration, 15s by default")
    args = parser.parse_args()
    queues = [args.queue] if args.queue else [20, 1000]
    results = [run_case(queue, args.duration) for queue in queues]
    print_summary(results)

if __name__ == "__main__":
    setLogLevel("info")
    sys.exit(main())
