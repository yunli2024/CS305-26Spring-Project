from collections import defaultdict, deque

from os_ken.base import app_manager
from os_ken.controller import ofp_event
from os_ken.controller.handler import MAIN_DISPATCHER, set_ev_cls
from os_ken.lib.packet import arp, dhcp, ethernet, ether_types, packet
from os_ken.ofproto import inet, ofproto_v1_0
from os_ken.topology import event
from os_ken.topology.api import get_link
from os_ken.topology.switches import LLDPPacket, Switches

from dhcp import DHCPServer
from dns_server import DNSServer
from firewall import Firewall
from ofctl_utilis import OfCtl, VLANID_NONE


class ControllerApp(app_manager.OSKenApp):
    OFP_VERSIONS = [ofproto_v1_0.OFP_VERSION]
    _CONTEXTS = {"switches": Switches}

    def __init__(self, *args, **kwargs):
        super(ControllerApp, self).__init__(*args, **kwargs)
        self.switches = kwargs.get("switches")
        self.datapaths = {}
        self.ofctls = {}
        self.hosts = {}
        self.ip_to_mac = {}
        self.links = defaultdict(set)
        self.link_ports = {}
        self.down_ports = set()
        self.forward_macs = set()
        self.logged_paths = {}
        self.firewall = Firewall()
        self.dns_server = DNSServer()

    @set_ev_cls(event.EventSwitchEnter)
    def handle_switch_add(self, ev):
        datapath = ev.switch.dp
        if not self.datapaths:
            DHCPServer.reset()

        self.datapaths[datapath.id] = datapath
        self.ofctls[datapath.id] = OfCtl.factory(datapath, self.logger)

        self.install_controller_flows(datapath)
        self.dns_server.install_packetin_flow(
            datapath, self.ofctls[datapath.id], ether_types, inet, VLANID_NONE
        )
        self.clear_logged_paths()
        self.firewall.installed = {
            key for key in self.firewall.installed if key[0] != datapath.id
        }
        self.firewall.install_rules(self.ofctls)
        self.refresh_forwarding_rules()

    @set_ev_cls(event.EventSwitchLeave)
    def handle_switch_delete(self, ev):
        dpid = ev.switch.dp.id
        self.datapaths.pop(dpid, None)
        self.ofctls.pop(dpid, None)
        self.firewall.installed = {
            key for key in self.firewall.installed if key[0] != dpid
        }
        self.links.pop(dpid, None)
        for neighbors in self.links.values():
            neighbors.discard(dpid)
        self.down_ports = {key for key in self.down_ports if key[0] != dpid}
        for key in list(self.link_ports):
            if dpid in key:
                self.link_ports.pop(key, None)
        for mac, host in list(self.hosts.items()):
            if host["dpid"] == dpid:
                self.hosts.pop(mac, None)
                if host["ip"]:
                    self.ip_to_mac.pop(host["ip"], None)
        self.clear_logged_paths()
        self.refresh_forwarding_rules()

    @set_ev_cls(event.EventHostAdd)
    def handle_host_add(self, ev):
        host = ev.host
        ip = host.ipv4[0] if host.ipv4 else None
        self.learn_host(host.mac, ip, host.port.dpid, host.port.port_no)
        self.refresh_forwarding_rules()

    @set_ev_cls(event.EventLinkAdd)
    def handle_link_add(self, ev):
        self.record_link(ev.link.src, ev.link.dst)
        self.clear_logged_paths()
        self.refresh_forwarding_rules()

    @set_ev_cls(event.EventLinkDelete)
    def handle_link_delete(self, ev):
        # LLDP-based delete events can be transient under Mininet timing.
        # Real link-down handling is done in handle_port_modify().
        return

    @set_ev_cls(event.EventPortModify)
    def handle_port_modify(self, ev):
        port = ev.port
        if not port.is_down():
            self.down_ports.discard((port.dpid, port.port_no))
            self.clear_logged_paths()
            self.refresh_forwarding_rules()
            return

        self.down_ports.add((port.dpid, port.port_no))
        for key in list(self.link_ports):
            if key[0] == port.dpid and self.link_ports[key] == port.port_no:
                self.links[key[0]].discard(key[1])
                self.link_ports.pop(key, None)

        for mac, host in list(self.hosts.items()):
            if host["dpid"] == port.dpid and host["port"] == port.port_no:
                self.hosts.pop(mac, None)
                if host["ip"]:
                    self.ip_to_mac.pop(host["ip"], None)
        self.clear_logged_paths()
        self.refresh_forwarding_rules()

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        try:
            msg = ev.msg
            datapath = msg.datapath
            in_port = msg.in_port
            pkt = packet.Packet(data=msg.data)

            if pkt.get_protocols(dhcp.dhcp):
                DHCPServer.handle_dhcp(datapath, in_port, pkt)
                return

            eth = pkt.get_protocol(ethernet.ethernet)
            if eth and eth.ethertype == ether_types.ETH_TYPE_LLDP:
                self.handle_lldp(datapath, in_port, msg.data)
                return

            pkt_arp = pkt.get_protocol(arp.arp)
            if pkt_arp:
                self.handle_arp(datapath, in_port, pkt_arp)
                return

            if self.dns_server.handle_dns(datapath, in_port, pkt):
                return
        except Exception as exc:
            self.logger.error("packet_in failed: %s", exc)

    def install_controller_flows(self, datapath):
        ofctl = self.ofctls.get(datapath.id)
        if ofctl is None:
            return
        ofctl.set_packetin_flow(
            cookie=0,
            priority=1000,
            dl_type=ether_types.ETH_TYPE_ARP,
            dl_vlan=VLANID_NONE,
        )

    def handle_lldp(self, datapath, in_port, data):
        try:
            src_dpid, src_port = LLDPPacket.lldp_parse(data)
        except LLDPPacket.LLDPUnknownFormat:
            return
        if src_dpid != datapath.id:
            self.record_link_values(src_dpid, src_port, datapath.id, in_port)
            self.refresh_forwarding_rules()

    def handle_arp(self, datapath, in_port, pkt_arp):
        self.learn_host(pkt_arp.src_mac, pkt_arp.src_ip, datapath.id, in_port)

        if pkt_arp.opcode != arp.ARP_REQUEST:
            self.refresh_forwarding_rules()
            return

        if self.dns_server.is_dns_ip(pkt_arp.dst_ip):
            self.send_arp_reply(
                datapath,
                in_port,
                pkt_arp,
                self.dns_server.server_mac,
                pkt_arp.dst_ip,
            )
            self.refresh_forwarding_rules()
            return

        target_mac = self.ip_to_mac.get(pkt_arp.dst_ip)
        if target_mac is None:
            self.logger.info("Unknown ARP target %s from %s",
                             pkt_arp.dst_ip, pkt_arp.src_ip)
            return

        self.send_arp_reply(datapath, in_port, pkt_arp, target_mac, pkt_arp.dst_ip)
        self.refresh_forwarding_rules()

    def send_arp_reply(self, datapath, out_port, request, sender_mac, sender_ip):
        ofctl = self.ofctls.get(datapath.id)
        if ofctl is None:
            return
        ofctl.send_arp(
            arp.ARP_REPLY,
            VLANID_NONE,
            request.src_mac,
            sender_mac,
            sender_ip,
            request.src_ip,
            request.src_mac,
            datapath.ofproto.OFPP_CONTROLLER,
            out_port,
        )

    def learn_host(self, mac, ip, dpid, port):
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
        if old != self.hosts[mac]:
            self.clear_logged_paths(mac)

    def record_link(self, src, dst):
        self.record_link_values(src.dpid, src.port_no, dst.dpid, dst.port_no)

    def record_link_values(self, src_dpid, src_port, dst_dpid, dst_port):
        if src_dpid == dst_dpid:
            return
        if (src_dpid, src_port) in self.down_ports:
            return
        if (dst_dpid, dst_port) in self.down_ports:
            return
        self.links[src_dpid].add(dst_dpid)
        self.links[dst_dpid].add(src_dpid)
        self.link_ports[(src_dpid, dst_dpid)] = src_port
        self.link_ports[(dst_dpid, src_dpid)] = dst_port

    def remove_link(self, src, dst):
        self.links[src.dpid].discard(dst.dpid)
        self.links[dst.dpid].discard(src.dpid)
        self.link_ports.pop((src.dpid, dst.dpid), None)
        self.link_ports.pop((dst.dpid, src.dpid), None)

    def sync_topology_links(self):
        if self.switches is not None:
            for link in list(self.switches.links):
                self.record_link(link.src, link.dst)

        try:
            discovered = get_link(self, None)
        except Exception as exc:
            self.logger.debug("get_link failed: %s", exc)
            return

        for link in discovered:
            self.record_link(link.src, link.dst)

    def shortest_path(self, src_dpid, dst_dpid):
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

    def refresh_forwarding_rules(self):
        self.sync_topology_links()
        self.clear_forwarding_rules()

        for dst_mac, dst_host in self.hosts.items():
            dst_dpid = dst_host["dpid"]
            dst_port = dst_host["port"]
            if dst_dpid not in self.datapaths:
                continue

            for src_dpid, datapath in self.datapaths.items():
                path = self.shortest_path(src_dpid, dst_dpid)
                if not path:
                    continue
                if src_dpid == dst_dpid:
                    out_port = dst_port
                else:
                    out_port = self.link_ports.get((src_dpid, path[1]))
                    if out_port is None:
                        continue
                self.install_forwarding_rule(datapath, dst_mac, out_port)

            self.log_host_paths(dst_mac)

        self.forward_macs = set(self.hosts)

    def clear_forwarding_rules(self):
        for dst_mac in self.forward_macs | set(self.hosts):
            for datapath in self.datapaths.values():
                self.delete_forwarding_rule(datapath, dst_mac)

    def delete_forwarding_rule(self, datapath, dst_mac):
        ofctl = self.ofctls.get(datapath.id)
        if ofctl is None:
            return
        parser = datapath.ofproto_parser
        ofproto = datapath.ofproto
        wildcards = ofproto.OFPFW_ALL & ~ofproto.OFPFW_DL_DST
        match = parser.OFPMatch(wildcards, 0, 0, dst_mac, 0, 0, 0,
                                0, 0, 0, 0, 0, 0)
        ofctl.delete_flow(cookie=0, priority=100, match=match)

    def install_forwarding_rule(self, datapath, dst_mac, out_port):
        ofctl = self.ofctls.get(datapath.id)
        if ofctl is None:
            return
        actions = [datapath.ofproto_parser.OFPActionOutput(out_port)]
        ofctl.set_flow(
            cookie=0,
            priority=100,
            dl_dst=dst_mac,
            dl_vlan=VLANID_NONE,
            actions=actions,
        )

    def log_host_paths(self, dst_mac):
        dst_host = self.hosts.get(dst_mac)
        if not dst_host:
            return

        for src_mac, src_host in sorted(self.hosts.items()):
            if src_mac == dst_mac:
                continue
            path = self.shortest_path(src_host["dpid"], dst_host["dpid"])
            if not path:
                continue
            signature = tuple(path)
            key = (src_mac, dst_mac)
            if self.logged_paths.get(key) == signature:
                continue
            self.logged_paths[key] = signature
            full_path = [src_mac] + ["s%s" % dpid for dpid in path] + [dst_mac]
            self.logger.info(
                "Shortest path %s -> %s: %s, distance=%s",
                src_mac,
                dst_mac,
                " -> ".join(full_path),
                len(full_path) - 1,
            )

    def clear_logged_paths(self, mac=None):
        if mac is None:
            self.logged_paths.clear()
            return
        for key in list(self.logged_paths):
            if mac in key:
                self.logged_paths.pop(key, None)
