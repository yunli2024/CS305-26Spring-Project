import json
import socket
import struct


class DNSParseError(ValueError):
    pass


class DNSServer:
    DEFAULT_SERVER_IP = "192.168.1.1"
    DEFAULT_SERVER_MAC = "7e:49:b3:f0:f9:99"
    DEFAULT_TTL = 60
    COOKIE = 0x305D
    PACKETIN_PRIORITY = 2000
    DNS_PORT = 53

    def __init__(self, record_file="dns_records.json"):
        self.record_file = record_file
        self.server_ip = self.DEFAULT_SERVER_IP
        self.server_mac = self.DEFAULT_SERVER_MAC
        self.ttl = self.DEFAULT_TTL
        self.records = {}
        self._load_records(record_file)

    def _load_records(self, record_file):
        try:
            with open(record_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            data = {}

        self.server_ip = data.get("server_ip", self.server_ip)
        self.server_mac = data.get("server_mac", self.server_mac)
        self.ttl = int(data.get("ttl", self.ttl))
        self.records = {
            self._normalize_name(name): ip
            for name, ip in data.get("records", {}).items()
        }

    def is_dns_ip(self, ip):
        return ip == self.server_ip

    def install_packetin_flow(self, datapath, ofctl, ether_types, inet, vlan_id):
        actions = [
            datapath.ofproto_parser.OFPActionOutput(
                datapath.ofproto.OFPP_CONTROLLER,
                0xFFFF,
            )
        ]
        ofctl.set_flow(
            cookie=self.COOKIE,
            priority=self.PACKETIN_PRIORITY,
            dl_type=ether_types.ETH_TYPE_IP,
            dl_vlan=vlan_id,
            nw_proto=inet.IPPROTO_UDP,
            tp_dst=self.DNS_PORT,
            actions=actions,
        )

    def handle_dns(self, datapath, in_port, pkt):
        try:
            from os_ken.lib.packet import ethernet, ipv4, udp
        except ImportError:
            return False

        eth_pkt = pkt.get_protocol(ethernet.ethernet)
        ip_pkt = pkt.get_protocol(ipv4.ipv4)
        udp_pkt = pkt.get_protocol(udp.udp)
        if not eth_pkt or not ip_pkt or not udp_pkt:
            return False
        if udp_pkt.dst_port != self.DNS_PORT or ip_pkt.dst != self.server_ip:
            return False

        payload = self._extract_udp_payload(pkt.data)
        if not payload:
            return False

        try:
            dns_response = self.build_response(payload)
        except DNSParseError:
            return True

        frame = self._build_ipv4_udp_frame(
            src_mac=self.server_mac,
            dst_mac=eth_pkt.src,
            src_ip=self.server_ip,
            dst_ip=ip_pkt.src,
            src_port=self.DNS_PORT,
            dst_port=udp_pkt.src_port,
            payload=dns_response,
        )
        self._send_packet(datapath, in_port, frame)
        return True

    def build_response(self, query):
        if len(query) < 12:
            raise DNSParseError("DNS query header is incomplete")

        query_id, flags, qdcount, _, _, _ = struct.unpack("!HHHHHH", query[:12])
        if qdcount < 1:
            raise DNSParseError("DNS query has no question")

        question_end, qname = self._read_qname(query, 12)
        if question_end + 4 > len(query):
            raise DNSParseError("DNS question is incomplete")

        question = query[12:question_end + 4]
        qtype, qclass = struct.unpack("!HH", query[question_end:question_end + 4])
        answer_ip = self.records.get(self._normalize_name(qname))

        answer = b""
        rcode = 0
        if qtype == 1 and qclass == 1 and answer_ip:
            answer = self._build_a_answer(answer_ip)
        elif qtype == 1 and qclass == 1:
            rcode = 3

        response_flags = 0x8000 | 0x0400 | (flags & 0x0100) | rcode
        header = struct.pack(
            "!HHHHHH",
            query_id,
            response_flags,
            1,
            1 if answer else 0,
            0,
            0,
        )
        return header + question + answer

    def _build_a_answer(self, ip):
        return (
            b"\xC0\x0C"
            + struct.pack("!HHIH", 1, 1, self.ttl, 4)
            + socket.inet_aton(ip)
        )

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

    def _normalize_name(self, name):
        return str(name).strip().rstrip(".").lower()

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

    def _build_ipv4_udp_frame(self, src_mac, dst_mac, src_ip, dst_ip,
                              src_port, dst_port, payload):
        udp_length = 8 + len(payload)
        ip_length = 20 + udp_length
        ip_header = struct.pack(
            "!BBHHHBBH4s4s",
            0x45,
            0,
            ip_length,
            0,
            0,
            64,
            17,
            0,
            socket.inet_aton(src_ip),
            socket.inet_aton(dst_ip),
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

    def _ipv4_checksum(self, header):
        if len(header) % 2:
            header += b"\x00"
        total = 0
        for i in range(0, len(header), 2):
            total += (header[i] << 8) + header[i + 1]
            total = (total & 0xFFFF) + (total >> 16)
        return (~total) & 0xFFFF
