import time
from os_ken.lib import addrconv #P地址和MAC地址的转换
from os_ken.lib.packet import packet 
from os_ken.lib.packet import ethernet #封装以太网首部
from os_ken.lib.packet import ipv4 #封装 IPv4 首部
from os_ken.lib.packet import udp #封装 UDP 首部
from os_ken.lib.packet import dhcp #创建和解析 DHCP 报文


class Config():
    controller_macAddr = '7e:49:b3:f0:f9:99'   # don't modify, a dummy mac address for fill the mac enrty
    dns = '192.168.1.1' # controller-hosted DNS server for bonus 4
    start_ip = '192.168.1.2' # can be modified
    end_ip = '192.168.1.100' # can be modified
    netmask = '255.255.255.0' #can be modified
    lease_time = 86400  #bonus1: 1 day in seconds IP有效时长


class DHCPServer():
    #从配置文件读取网络参数
    hardware_addr = Config.controller_macAddr
    start_ip = Config.start_ip
    end_ip = Config.end_ip
    netmask = Config.netmask
    dns = Config.dns
    lease_time = Config.lease_time

    # bonus2: RFC-compliant state tables 
    # mac  -> ip  (tentative offer, before REQUEST is received)
    pending_offers = {}
    # mac  -> ip  (confirmed leases after ACK)
    ip_pool = {}
    # ip   -> expiry_timestamp  (tracks lease expiry for reclamation)
    lease_expiry = {}
    

    # help tool

    #计算范围内的ip 返回列表
    @classmethod
    def _build_ip_range(cls):
        start = cls._ip_to_int(cls.start_ip) 
        end   = cls._ip_to_int(cls.end_ip)
        return [cls._int_to_ip(i) for i in range(start, end + 1)]

    #ip转成数字
    @staticmethod
    def _ip_to_int(ip_str):
        p = list(map(int, ip_str.split('.')))
        return (p[0] << 24) | (p[1] << 16) | (p[2] << 8) | p[3]

    #数字转ip(str)
    @staticmethod
    def _int_to_ip(n):
        return '{}.{}.{}.{}'.format(
            (n >> 24) & 0xFF, (n >> 16) & 0xFF, (n >> 8) & 0xFF, n & 0xFF)
    
    #检查指定ip是否过期，过期返回true
    @classmethod
    def _is_expired(cls, ip):
        expiry = cls.lease_expiry.get(ip)
        return expiry is not None and time.time() > expiry

    #回收过期ip bonus 1+2
    @classmethod
    def _reclaim_expired(cls):
        expired_macs = []
        for mac, ip in cls.ip_pool.items():
            if cls._is_expired(ip):
                    expired_macs.append(mac)
        for mac in expired_macs:
            ip = cls.ip_pool.pop(mac)  #删除mac对应的ip 返回ip
            cls.lease_expiry.pop(ip, None) #ip不存在返回none

    #统计并返回已经被占用了的 IP 地址
    @classmethod
    def _in_use_ips(cls):
        return set(cls.ip_pool.values()) | set(cls.pending_offers.values()) #offer+ack并集

    #分配地址 bonus 2
    '''
    已有：续租；有意向；新ip
    '''
    @classmethod
    def _allocate_ip(cls, client_mac):
        #从过期ip释放ip资源
        cls._reclaim_expired()    

        #1.已有
        if client_mac in cls.ip_pool:
            return cls.ip_pool[client_mac]

        #2.已offer
        if client_mac in cls.pending_offers:
            return cls.pending_offers[client_mac]

        #3.分配新ip
        in_use = cls._in_use_ips()
        for ip in cls._build_ip_range():
            if ip not in in_use:
                return ip

        #4.耗尽
        return None  # pool exhausted


    #RFC flow

    #获取消息类型
    #msg:1discover 3request
    @classmethod
    def _get_msg_type(cls, dhcp_pkt):
        for opt in dhcp_pkt.options.option_list:
            if opt.tag == dhcp.DHCP_MESSAGE_TYPE_OPT:
                return opt.value[0]
        return None

    #option list构建 gateway构建 返回元组
    #msg:2offer 5ack
    @classmethod
    def _build_options(cls, msg_type, offered_ip):
        subnet_base = '.'.join(offered_ip.split('.')[:3]) #子网掩码固定的写法 不严谨
        gateway_ip  = subnet_base + '.1'
        return dhcp.options(option_list=[
            dhcp.option(tag=dhcp.DHCP_MESSAGE_TYPE_OPT,
                        value=bytes([msg_type])),
            dhcp.option(tag=dhcp.DHCP_SUBNET_MASK_OPT,
                        value=addrconv.ipv4.text_to_bin(cls.netmask)),
            dhcp.option(tag=dhcp.DHCP_GATEWAY_ADDR_OPT,
                        value=addrconv.ipv4.text_to_bin(gateway_ip)),
            dhcp.option(tag=dhcp.DHCP_DNS_SERVER_ADDR_OPT,
                        value=addrconv.ipv4.text_to_bin(cls.dns)),
            dhcp.option(tag=dhcp.DHCP_IP_ADDR_LEASE_TIME_OPT,  # Bonus 1
                        value=cls.lease_time.to_bytes(4, byteorder='big')), #四字节 大端序
            dhcp.option(tag=dhcp.DHCP_SERVER_IDENTIFIER_OPT,
                        value=addrconv.ipv4.text_to_bin(gateway_ip)),
        ]), gateway_ip

    #封装包
    @classmethod
    def _build_reply_pkt(cls, client_mac, xid, offered_ip, msg_type):
        options, gateway_ip = cls._build_options(msg_type, offered_ip)
        dhcp_pkt = dhcp.dhcp(
            op=dhcp.DHCP_BOOT_REPLY,
            chaddr=client_mac,
            siaddr=gateway_ip,
            boot_file='', #启动文件名
            yiaddr=offered_ip,
            xid=xid,
            options=options,
        )
        pkt = packet.Packet()
        pkt.add_protocol(ethernet.ethernet(
            src=cls.hardware_addr, dst='ff:ff:ff:ff:ff:ff', ethertype=0x0800))
        pkt.add_protocol(ipv4.ipv4(
            src=gateway_ip, dst='255.255.255.255', proto=17))
        pkt.add_protocol(udp.udp(src_port=67, dst_port=68))
        pkt.add_protocol(dhcp_pkt)
        return pkt


    #handle

    #DISCOVER → OFFER.  Records a pending offer
    @classmethod
    def assemble_offer(cls, pkt, datapath):
        eth_pkt  = pkt.get_protocol(ethernet.ethernet)
        dhcp_pkt = pkt.get_protocol(dhcp.dhcp)
        client_mac = eth_pkt.src

        offered_ip = cls._allocate_ip(client_mac)
        if offered_ip is None:
            return None

        # Bonus 2
        cls.pending_offers[client_mac] = offered_ip
        return cls._build_reply_pkt(client_mac, dhcp_pkt.xid,
                                    offered_ip, dhcp.DHCP_OFFER)

    #REQUEST → ACK. Promotes pending offer to a confirmed, timed lease.
    @classmethod
    def assemble_ack(cls, pkt, datapath, port):
        eth_pkt  = pkt.get_protocol(ethernet.ethernet)
        dhcp_pkt = pkt.get_protocol(dhcp.dhcp)
        client_mac = eth_pkt.src

        assigned_ip = cls._allocate_ip(client_mac)
        if assigned_ip is None:
            return None

        # Bonus 1 + 2
        cls.ip_pool[client_mac] = assigned_ip
        cls.lease_expiry[assigned_ip] = time.time() + cls.lease_time
        cls.pending_offers.pop(client_mac, None)  

        return cls._build_reply_pkt(client_mac, dhcp_pkt.xid,
                                    assigned_ip, dhcp.DHCP_ACK)

    '''
    DISCOVER → OFFER
    REQUEST  → ACK
    RELEASE  → free the lease immediately  (Bonus 2)
    '''
    @classmethod
    def handle_dhcp(cls, datapath, port, pkt):
        dhcp_pkt = pkt.get_protocol(dhcp.dhcp)
        if dhcp_pkt is None:
            return

        msg_type = cls._get_msg_type(dhcp_pkt)

        if msg_type == dhcp.DHCP_DISCOVER:
            reply = cls.assemble_offer(pkt, datapath)
            if reply:
                cls._send_packet(datapath, port, reply)

        elif msg_type == dhcp.DHCP_REQUEST:
            reply = cls.assemble_ack(pkt, datapath, port)
            if reply:
                cls._send_packet(datapath, port, reply)

        #bonus 1+2
        elif msg_type == dhcp.DHCP_RELEASE:
            eth_pkt = pkt.get_protocol(ethernet.ethernet)
            client_mac = eth_pkt.src
            ip = cls.ip_pool.pop(client_mac, None)
            if ip:
                cls.lease_expiry.pop(ip, None)


    #send


    @classmethod
    def _send_packet(cls, datapath, port, pkt):
        ofproto = datapath.ofproto #OpenFlow 协议版本的所有常量和状态码
        parser  = datapath.ofproto_parser #把命令翻译成OpenFlow字节码
        if isinstance(pkt, str):
            pkt = pkt.encode() #转为二进制
        pkt.serialize() #序列化
        data = pkt.data
        actions = [parser.OFPActionOutput(port=port)]
        out = parser.OFPPacketOut(datapath=datapath,
                                  buffer_id=ofproto.OFP_NO_BUFFER, #无缓存
                                  in_port=ofproto.OFPP_CONTROLLER, #流入端口
                                  actions=actions,
                                  data=data)
        datapath.send_msg(out)
