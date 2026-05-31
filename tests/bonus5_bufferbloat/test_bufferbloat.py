import argparse
import re
import sys
import time

from mininet.link import TCLink
from mininet.log import setLogLevel
from mininet.net import Mininet
from mininet.node import OVSBridge
from mininet.topo import Topo


"""
Bonus5: Bufferbloat 实验脚本。

这个脚本不依赖 controller.py，也不需要 os-ken controller。
它只使用 Mininet 自带的 OVSBridge 和 TCLink 搭建一个二层网络，
通过改变瓶颈链路的队列大小，观察“大队列会让延迟明显升高”的现象。

实验思路：
1. h1 -> h3 运行 iperf，制造持续 TCP 大流量。
2. h2 -> h4 同时运行 ping，模拟小的交互流量。
3. 两种流量都要经过 s1-s2 这条瓶颈链路。
4. 对比小队列和大队列时 ping RTT 的变化。
"""


class BufferbloatTopo(Topo):
    """
    一个简单的 dumbbell 拓扑。

        h1 ----+
               |
               s1 ---- s2 ---- h3
               |       |
        h2 ----+       +---- h4

    h1 到 h3：用 iperf 发送 TCP 大流量。
    h2 到 h4：用 ping 测量延迟。

    两条流都会经过 s1-s2，所以 s1-s2 是共同的瓶颈链路。
    如果 s1-s2 的队列很大，TCP 包会大量堆积，ping 包也要排队，
    于是 ping RTT 会明显升高，这就是 Bufferbloat 的核心现象。
    """

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
    最后一行通常是汇总结果，所以这里取最后一个匹配值。

    为了方便比较，函数统一返回 Mbits/sec：
    - Kbits/sec 会除以 1000；
    - Gbits/sec 会乘以 1000；
    - Mbits/sec 直接返回。
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
    让某个主机 ping 目标 IP。

    - count 控制发送多少个 ICMP 包；
    - -i 0.2 表示每 0.2 秒发一个包；
    - -W 2 表示每个包最多等待 2 秒。
    """
    return host.cmd("ping -c %s -i 0.2 -W 2 %s" % (count, dst_ip))


def run_case(queue_size, duration):
    """
    运行一次指定queue_size的实验。

    参数：
    - queue_size：瓶颈链路 s1-s2 的队列大小。
    - duration：iperf TCP 大流量持续时间。

    实验流程：
    1. 启动 Mininet 网络。
    2. 先在没有 TCP 大流量时测一次 h2 -> h4 的基础 RTT。
    3. 在 h3 上启动 iperf server。
    4. h2 -> h4 后台运行 ping。
    5. h1 -> h3 前台运行 iperf，制造 TCP 大流量。
    6. 读取 ping 和 iperf 输出，提取 RTT 与吞吐量。
    """
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

        # 第一次 ping：网络空闲状态下的延迟。
        # 这个值作为 baseline，说明链路本身的基础 RTT 大概是多少。
        idle_ping = ping(h2, h4.IP())
        idle_loss, idle_rtt = parse_ping(idle_ping)

        # 启动 iperf server。
        # pkill 是为了避免上一次实验留下旧的 iperf server。
        h3.cmd("pkill -f 'iperf -s' || true")
        h3.cmd("iperf -s -p 5001 >/tmp/bonus5_iperf_server.log 2>&1 &")
        time.sleep(1)

        # 后台启动 ping。
        # 它会和后面的 iperf 同时经过瓶颈链路，用来观察拥塞时的小包延迟。
        h2.cmd("ping -c 30 -i 0.5 -W 2 %s >/tmp/bonus5_ping.log 2>&1 &" % h4.IP())
        time.sleep(1)

        # 前台启动 TCP 大流量。
        # 这里会阻塞 duration 秒，期间 h2 的 ping 正在后台持续运行。
        iperf_output = h1.cmd("iperf -c %s -p 5001 -t %s" % (h3.IP(), duration))
        time.sleep(1)

        # 读取后台 ping 的完整输出。
        busy_ping = h2.cmd("cat /tmp/bonus5_ping.log")

        busy_loss, busy_rtt = parse_ping(busy_ping)
        throughput = parse_iperf(iperf_output)

        print("Idle ping loss: %s%%" % idle_loss)
        print("Idle ping avg RTT: %s ms" % idle_rtt)
        print("TCP throughput: %s Mbits/sec" % throughput)
        print("Busy ping loss: %s%%" % busy_loss)
        print("Busy ping avg RTT: %s ms" % busy_rtt)

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
    """
    打印最终汇总表。

    重点看 busy_rtt：
    如果大队列的 busy_rtt 明显高于小队列，
    就说明 TCP 大流量在大队列中排队，造成了 Bufferbloat。
    """
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
    """
    程序入口。

    默认不加参数时会自动测试两组：
    - queue=20：小队列；
    - queue=1000：大队列。

    也可以通过 --queue 只测一组，方便单独截图或调试。
    """
    parser = argparse.ArgumentParser(description="Bonus5 Bufferbloat Mininet test")
    parser.add_argument("--queue", type=int, help="只测试一个队列大小，例如 20 或 1000")
    parser.add_argument("--duration", type=int, default=15, help="iperf 持续时间，默认 15 秒")
    args = parser.parse_args()

    queues = [args.queue] if args.queue else [20, 1000]
    results = [run_case(queue, args.duration) for queue in queues]
    print_summary(results)


if __name__ == "__main__":
    setLogLevel("info")
    sys.exit(main())
