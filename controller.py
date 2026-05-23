from os_ken.base import app_manager
from os_ken.controller import ofp_event
from os_ken.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER
from os_ken.controller.handler import set_ev_cls
from os_ken.topology import event
from os_ken.topology.switches import Switch, Host, HostState, Port, PortState, PortData, PortDataState, Link, LinkState
from os_ken.topology.switches import Switches
from os_ken.ofproto import ofproto_v1_0, ether, inet
from os_ken.lib.packet import packet, ethernet, ether_types, arp
from os_ken.lib.packet import dhcp
from os_ken.lib.packet import ethernet
from os_ken.lib.packet import ipv4
from os_ken.lib.packet import packet
from os_ken.lib.packet import udp
from dhcp import DHCPServer
from collections import defaultdict, deque
import time
from ofctl_utilis import OfCtl,OfCtl_v1_0,OfCtl_after_v1_2,VLANID_NONE
import logging
import copy
import heapq
from firewall import Firewall
from dns_server import DNSServer


class ControllerApp(app_manager.OSKenApp):
    OFP_VERSIONS = [ofproto_v1_0.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(ControllerApp, self).__init__(*args, **kwargs)
        self.datapaths = {}
        self.ofctls = {}
        self.hosts = {}
        self.ip_to_mac = {}
        self.links = defaultdict(set)
        self.link_ports = {}
        self.forward_macs = set()
        self.firewall = Firewall()
        self.dns_server = DNSServer()

    @set_ev_cls(event.EventSwitchEnter)
    def handle_switch_add(self, ev):
        """
        Event handler indicating a switch has come online.
        """
        datapath = ev.switch.dp
        self.datapaths[datapath.id] = datapath
        self.ofctls[datapath.id] = OfCtl.factory(datapath, self.logger)
        self._install_controller_flows(datapath)
        # 将DNS的flow 安装到交换机上，使得会将DNS query交给controller
        self.dns_server.install_packetin_flow(datapath, self.ofctls[datapath.id],
                                              ether_types, inet, VLANID_NONE)
        self.firewall.install_rules(self.ofctls)
        self._refresh_forwarding_rules()

    @set_ev_cls(event.EventSwitchLeave)
    def handle_switch_delete(self, ev):
        """
        Event handler indicating a switch has been removed
        """
        dpid = ev.switch.dp.id
        self.datapaths.pop(dpid, None)
        self.ofctls.pop(dpid, None)
        self.links.pop(dpid, None)
        for neighbors in self.links.values():
            neighbors.discard(dpid)
        for key in list(self.link_ports):
            if dpid in key:
                self.link_ports.pop(key, None)
        for mac, host in list(self.hosts.items()):
            if host["dpid"] == dpid:
                self.hosts.pop(mac, None)
                self.ip_to_mac.pop(host["ip"], None)
        self._refresh_forwarding_rules()


    @set_ev_cls(event.EventHostAdd)
    def handle_host_add(self, ev):
        """
        Event handler indiciating a host has joined the network
        This handler is automatically triggered when a host sends an ARP response.
        """ 
        host = ev.host
        ip = host.ipv4[0] if host.ipv4 else None
        self._learn_host(host.mac, ip, host.port.dpid, host.port.port_no)
        self._refresh_forwarding_rules()

    @set_ev_cls(event.EventLinkAdd)
    def handle_link_add(self, ev):
        """
        Event handler indicating a link between two switches has been added
        """
        src = ev.link.src
        dst = ev.link.dst
        self.links[src.dpid].add(dst.dpid)
        self.link_ports[(src.dpid, dst.dpid)] = src.port_no
        self._refresh_forwarding_rules()

    @set_ev_cls(event.EventLinkDelete)
    def handle_link_delete(self, ev):
        """
        Event handler indicating when a link between two switches has been deleted
        """
        src = ev.link.src
        dst = ev.link.dst
        self.links[src.dpid].discard(dst.dpid)
        self.link_ports.pop((src.dpid, dst.dpid), None)
        self._refresh_forwarding_rules()
   
        

    @set_ev_cls(event.EventPortModify)
    def handle_port_modify(self, ev):
        """
        Event handler for when any switch port changes state.
        This includes links for hosts as well as links between switches.
        """
        port = ev.port
        if port.is_down():
            for key in list(self.link_ports):
                if key[0] == port.dpid and self.link_ports[key] == port.port_no:
                    self.links[key[0]].discard(key[1])
                    self.link_ports.pop(key, None)
            for mac, host in list(self.hosts.items()):
                if host["dpid"] == port.dpid and host["port"] == port.port_no:
                    self.hosts.pop(mac, None)
                    if host["ip"]:
                        self.ip_to_mac.pop(host["ip"], None)
        self._refresh_forwarding_rules()



    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        try:
            msg = ev.msg
            datapath = msg.datapath
            pkt = packet.Packet(data=msg.data)
            pkt_dhcp = pkt.get_protocols(dhcp.dhcp)
            inPort = msg.in_port
            if not pkt_dhcp:
                self._handle_non_dhcp_packet(datapath, inPort, pkt)
            else:
                DHCPServer.handle_dhcp(datapath, inPort, pkt)      
            return 
        except Exception as e:
            self.logger.error(e)

    def _install_controller_flows(self, datapath):
        ofctl = self.ofctls.get(datapath.id)
        if ofctl is None:
            return
        ofctl.set_packetin_flow(cookie=0, priority=1000,
                                dl_type=ether_types.ETH_TYPE_ARP,
                                dl_vlan=VLANID_NONE)

    def _handle_non_dhcp_packet(self, datapath, in_port, pkt):
        pkt_arp = pkt.get_protocol(arp.arp)
        if pkt_arp:
            self._handle_arp(datapath, in_port, pkt_arp)
            return
        if self.dns_server.handle_dns(datapath, in_port, pkt):
            return

    def _handle_arp(self, datapath, in_port, pkt_arp):
        self._learn_host(pkt_arp.src_mac, pkt_arp.src_ip, datapath.id, in_port)

        if pkt_arp.opcode != arp.ARP_REQUEST:
            self._refresh_forwarding_rules()
            return

        # 如果arp的目标ip是dns_ip 就reply
        if self.dns_server.is_dns_ip(pkt_arp.dst_ip):
            ofctl = self.ofctls.get(datapath.id)
            if ofctl is None:
                return
            ofctl.send_arp(arp.ARP_REPLY, VLANID_NONE,
                           pkt_arp.src_mac,
                           self.dns_server.server_mac,
                           pkt_arp.dst_ip,
                           pkt_arp.src_ip,
                           pkt_arp.src_mac,
                           datapath.ofproto.OFPP_CONTROLLER,
                           in_port)
            self._refresh_forwarding_rules()
            return

        target_mac = self.ip_to_mac.get(pkt_arp.dst_ip)
        if target_mac is None:
            self.logger.info("Unknown ARP target %s from %s",
                             pkt_arp.dst_ip, pkt_arp.src_ip)
            return

        ofctl = self.ofctls.get(datapath.id)
        if ofctl is None:
            return
        ofctl.send_arp(arp.ARP_REPLY, VLANID_NONE,
                       pkt_arp.src_mac,
                       target_mac,
                       pkt_arp.dst_ip,
                       pkt_arp.src_ip,
                       pkt_arp.src_mac,
                       datapath.ofproto.OFPP_CONTROLLER,
                       in_port)
        self._refresh_forwarding_rules()

    def _learn_host(self, mac, ip, dpid, port):
        if not mac or mac == "ff:ff:ff:ff:ff:ff":
            return
        if ip == "0.0.0.0":
            ip = None

        old = self.hosts.get(mac)
        if old and old.get("ip") and old["ip"] != ip:
            self.ip_to_mac.pop(old["ip"], None)

        self.hosts[mac] = {"ip": ip, "dpid": dpid, "port": port}
        if ip:
            self.ip_to_mac[ip] = mac
        self.logger.info("Learned host mac=%s ip=%s at s%s:%s",
                         mac, ip, dpid, port)

    def _shortest_path(self, src_dpid, dst_dpid):
        if src_dpid == dst_dpid:
            return [src_dpid]
        queue = deque([(src_dpid, [src_dpid])])
        visited = {src_dpid}
        while queue:
            current, path = queue.popleft()
            for neighbor in sorted(self.links.get(current, [])):
                if neighbor in visited:
                    continue
                next_path = path + [neighbor]
                if neighbor == dst_dpid:
                    return next_path
                visited.add(neighbor)
                queue.append((neighbor, next_path))
        return None

    def _refresh_forwarding_rules(self):
        self._clear_forwarding_rules()
        for dst_mac, dst_host in self.hosts.items():
            dst_dpid = dst_host["dpid"]
            dst_port = dst_host["port"]
            if dst_dpid not in self.datapaths:
                continue

            for src_dpid, datapath in self.datapaths.items():
                path = self._shortest_path(src_dpid, dst_dpid)
                if not path:
                    continue
                if src_dpid == dst_dpid:
                    out_port = dst_port
                else:
                    out_port = self.link_ports.get((src_dpid, path[1]))
                    if out_port is None:
                        continue
                self._install_forwarding_rule(datapath, dst_mac, out_port)

            self._log_host_paths(dst_mac)
        self.forward_macs = set(self.hosts)

    def _clear_forwarding_rules(self):
        for dst_mac in self.forward_macs | set(self.hosts):
            for datapath in self.datapaths.values():
                self._delete_forwarding_rule(datapath, dst_mac)

    def _delete_forwarding_rule(self, datapath, dst_mac):
        ofctl = self.ofctls.get(datapath.id)
        if ofctl is None:
            return
        parser = datapath.ofproto_parser
        ofproto = datapath.ofproto
        wildcards = ofproto.OFPFW_ALL & ~ofproto.OFPFW_DL_DST
        match = parser.OFPMatch(wildcards, 0, 0, dst_mac, 0, 0, 0,
                                0, 0, 0, 0, 0, 0)
        ofctl.delete_flow(cookie=0, priority=100, match=match)

    def _install_forwarding_rule(self, datapath, dst_mac, out_port):
        ofctl = self.ofctls.get(datapath.id)
        if ofctl is None:
            return
        actions = [datapath.ofproto_parser.OFPActionOutput(out_port)]
        ofctl.set_flow(cookie=0, priority=100,
                       dl_dst=dst_mac,
                       dl_vlan=VLANID_NONE,
                       actions=actions)

    def _log_host_paths(self, dst_mac):
        dst_host = self.hosts.get(dst_mac)
        if not dst_host:
            return
        for src_mac, src_host in sorted(self.hosts.items()):
            if src_mac == dst_mac:
                continue
            path = self._shortest_path(src_host["dpid"], dst_host["dpid"])
            if path:
                full_path = [src_mac] + ["s%s" % dpid for dpid in path] + [dst_mac]
                distance = len(full_path) - 1
                self.logger.info("Shortest path %s -> %s: %s, distance=%s",
                                 src_mac, dst_mac, " -> ".join(full_path),
                                 distance)
    
