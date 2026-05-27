import json
import socket
import struct
from os_ken.lib.packet import ethernet, ipv4, udp

class DNSParseError(ValueError):
    pass

# DNSServer 类
class DNSServer:
    DEFAULT_SERVER_IP = "192.168.1.1"
    DEFAULT_SERVER_MAC = "7e:49:b3:f0:f9:99"
    DEFAULT_TTL = 60
    COOKIE = 0x305D # 安装flow时的cookie值
    PACKETIN_PRIORITY = 2000
    DNS_PORT = 53

    # 默认初始化的时候从默认字段加载
    def __init__(self, record_file="dns_records.json"):
        self.record_file = record_file
        self.server_ip = self.DEFAULT_SERVER_IP
        self.server_mac = self.DEFAULT_SERVER_MAC
        self.ttl = self.DEFAULT_TTL
        self.records = {}
        self._load_records(record_file)

    # 从json文件中提取data，可以对默认值进行覆盖。
    def _load_records(self, record_file):
        try:
            with open(record_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            data = {}

        self.server_ip = data.get("server_ip", self.server_ip)
        self.server_mac = data.get("server_mac", self.server_mac)
        self.ttl = int(data.get("ttl", self.ttl))
        # 将json文件中的域名解析表 (name,ip) 读进来
        self.records = {} # 创建records字典
        for name, ip in data.get("records", {}).items():
            normalized_name = self._normalize_name(name) # 将域名norm之后再写入
            self.records[normalized_name] = ip

    # 判断当前ip是否为DNS的虚拟ip的函数，给Controller的APR用
    def is_dns_ip(self, ip):
        return ip == self.server_ip

    # 给交换机安装flow table的规则：
    # 收到DNS请求(UDP dest port=53)的时候，统一送到Controller中集成的DNS server处理
    def install_packetin_flow(self, datapath, ofctl, ether_types, inet, vlan_id):
        # 定义action：把包送给Controller的这个端口
        actions = [
            datapath.ofproto_parser.OFPActionOutput(
                datapath.ofproto.OFPP_CONTROLLER,
                0xFFFF,
            )
        ]
        # 安装流表到交换机上
        ofctl.set_flow(
            cookie=self.COOKIE,
            priority=self.PACKETIN_PRIORITY, # 优先级高，就不会按照普通规则转发，而是送到Controller
            dl_type=ether_types.ETH_TYPE_IP,
            dl_vlan=vlan_id,
            nw_proto=inet.IPPROTO_UDP, # 只匹配UDP
            tp_dst=self.DNS_PORT, # 只匹配当前DNS的端口号，53
            actions=actions,
        )


    # 判断当前这个pkt包是不是发给controller中的DNS server的request
    # 如果是就构造DNS response 并且return True
    def handle_dns(self, datapath, in_port, pkt):
        # 从目标的eth ip udp头分别读取MAC IP port信息
        eth_pkt = pkt.get_protocol(ethernet.ethernet)
        ip_pkt = pkt.get_protocol(ipv4.ipv4)
        udp_pkt = pkt.get_protocol(udp.udp)
        # 信息缺失了，不是需要的直接False
        if not eth_pkt or not ip_pkt or not udp_pkt:
            return False
        # 如果不是我们DNS server的IP和port也False
        if udp_pkt.dst_port != self.DNS_PORT or ip_pkt.dst != self.server_ip:
            return False

        # 提取query的payload
        payload = self._extract_udp_payload(pkt.data)
        if not payload:
            return False

        # 使用payload的内容构造dns_response payload是作为DNS query参数
        try:
            dns_response = self.build_response(payload)
        except DNSParseError:
            return True # 如果构造失败了，但它确实是发给DNS server的包，返回True

        # 拿到response之后，构造完整的response以太网帧
        frame = self._build_ipv4_udp_frame(
            # 注意是从server到source的response 方向要注意
            src_mac=self.server_mac,
            dst_mac=eth_pkt.src,
            src_ip=self.server_ip,
            dst_ip=ip_pkt.src,
            src_port=self.DNS_PORT,
            dst_port=udp_pkt.src_port,
            payload=dns_response,
        )
        # 调用发送的函数 将response发回去
        self._send_packet(datapath, in_port, frame)
        return True


    # 手动构造DNS的response报文
    def build_response(self, query):
        if len(query) < 12: # DNS header有12字节
            raise DNSParseError("DNS query header is incomplete")

        # 从query中提取Transaction id,flags,qucount(问题数量，DNS查询计数器)
        query_id, flags, qdcount, _, _, _ = struct.unpack("!HHHHHH", query[:12])
        if qdcount < 1: # 没有查询问题，直接返回吧
            raise DNSParseError("DNS query has no question")

        # 从offset=12 开始读取要查询的域名qname
        # 域名结束的位置存在_end
        question_end, qname = self._read_qname(query, 12)

        # 注意，域名后面还要包含4bytes 否则不完整
        if question_end + 4 > len(query):
            raise DNSParseError("DNS question is incomplete")

        # 保存question的原样
        question = query[12:question_end + 4]
        qtype, qclass = struct.unpack("!HH", query[question_end:question_end + 4])
        # 去DNS name_ip 表格里面查这个待查询域名的IP是什么，作为返回的answer
        answer_ip = self.records.get(self._normalize_name(qname))

        # 如果answer_ip存在，就构建answer的内容
        # 如果不存在，就设置rcode=3 表示这次请求的域名不存在
        answer = b""
        rcode = 0
        if qtype == 1 and qclass == 1 and answer_ip:
            answer = self._build_a_answer(answer_ip)
        elif qtype == 1 and qclass == 1:
            rcode = 3

        response_flags = 0x8000 | 0x0400 | (flags & 0x0100) | rcode

        # 拼接 DNS response header
        header = struct.pack("!HHHHHH",query_id,response_flags,1,1 if answer else 0,0,0,)

        # 返回header和原样question以及查询到的answer
        return header + question + answer




    # 一些工具类
    # 根据ip构建answer
    def _build_a_answer(self, ip):
        return (
            b"\xC0\x0C"
            + struct.pack("!HHIH", 1, 1, self.ttl, 4)
            + socket.inet_aton(ip)
        )

    # 读取要查询的域名qname
    def _read_qname(self, data, offset):
        labels = []
        current = offset
        while current < len(data):
            length = data[current]
            current += 1
            if length == 0:
                return current, ".".join(labels)
            if length & 0xC0:
                raise DNSParseError("Compressed query names are not supported")
            if current + length > len(data):
                raise DNSParseError("DNS label exceeds payload length")
            labels.append(data[current:current + length].decode("ascii"))
            current += length
        raise DNSParseError("DNS name is not terminated")
    # 将域名规范化
    def _normalize_name(self, name):
        return str(name).strip().rstrip(".").lower()
    # 提取一个以太网帧中的payload部分，也就是真正的query
    def _extract_udp_payload(self, frame):
        if len(frame) < 42:
            return b""
        eth_type = struct.unpack("!H", frame[12:14])[0]
        if eth_type != 0x0800:
            return b""
        ihl = (frame[14] & 0x0F) * 4
        udp_start = 14 + ihl
        if len(frame) < udp_start + 8:
            return b""
        return frame[udp_start + 8:]

    # 构建完整的以太网帧
    def _build_ipv4_udp_frame(self, src_mac, dst_mac, src_ip, dst_ip,
                              src_port, dst_port, payload):
        udp_length = 8 + len(payload)
        ip_length = 20 + udp_length
        ip_header = struct.pack(
            "!BBHHHBBH4s4s",0x45,0,ip_length,0,0,64,17,0,socket.inet_aton(src_ip),socket.inet_aton(dst_ip),
        )
        checksum = self._ipv4_checksum(ip_header)
        ip_header = ip_header[:10] + struct.pack("!H", checksum) + ip_header[12:]
        udp_header = struct.pack("!HHHH", src_port, dst_port, udp_length, 0)
        return (
            self._mac_to_bytes(dst_mac)
            + self._mac_to_bytes(src_mac)
            + struct.pack("!H", 0x0800)
            + ip_header
            + udp_header
            + payload
        )

    # 将packet发送回去
    def _send_packet(self, datapath, in_port, data):
        parser = datapath.ofproto_parser
        ofproto = datapath.ofproto
        actions = [parser.OFPActionOutput(port=in_port)]
        out = parser.OFPPacketOut(
            datapath=datapath,
            buffer_id=ofproto.OFP_NO_BUFFER,
            in_port=ofproto.OFPP_CONTROLLER,
            actions=actions,
            data=data,
        )
        datapath.send_msg(out)

    def _mac_to_bytes(self, mac):
        return bytes(int(part, 16) for part in mac.split(":"))

    # checksum
    def _ipv4_checksum(self, header):
        if len(header) % 2:
            header += b"\x00"
        total = 0
        for i in range(0, len(header), 2):
            total += (header[i] << 8) + header[i + 1]
            total = (total & 0xFFFF) + (total >> 16)
        return (~total) & 0xFFFF
