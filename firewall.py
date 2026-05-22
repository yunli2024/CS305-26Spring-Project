# firewall.py

import json
import os
from dataclasses import dataclass

from os_ken.ofproto import ether, inet


@dataclass(frozen=True)
class FirewallRule:
    src_ip: str = None
    dst_ip: str = None
    proto: str = None
    src_port: object = None
    dst_port: object = None
    action: str = "deny"


class Firewall:
    COOKIE = 0x305F
    PRIORITY = 60000

    PROTO_MAP = {
        None: 0,
        "": 0,
        "*": 0,
        "any": 0,
        "icmp": inet.IPPROTO_ICMP,
        "tcp": inet.IPPROTO_TCP,
        "udp": inet.IPPROTO_UDP,
    }

    def __init__(self, rule_file="firewall_rule.json"):
        self.rule_file = rule_file
        self.rules = self._load_rules(rule_file)
        self.installed = set()

    # Some helper functions that may be useful
    def _normalize_any(self, value):
        if value is None:
            return None
        if isinstance(value, str) and value.strip().lower() in ["", "*", "any"]:
            return None
        return value

    def _normalize_proto(self, proto):
        proto = self._normalize_any(proto)
        if proto is None:
            return None
        return str(proto).lower()

    def _proto_to_number(self, proto):
        proto = self._normalize_proto(proto)
        return self.PROTO_MAP.get(proto, 0)

    def _normalize_port(self, value):
        value = self._normalize_any(value)
        if value is None:
            return 0
        return int(value)

    def _load_rules(self, rule_file):
        """
        Load firewall rules from firewall_rules.json and return a list of FirewallRule.
        """
        rules = []

        # TODO: read rule_file
        if not os.path.exists(rule_file):
            return rules
        with open(rule_file,"r") as f:
            data=json.load(f)
        
        # parse JSON rules
        rules_array=data.get("rules",[])
        # create FirewallRule objects
        for r in rules_array:
            if not isinstance(r,dict):
                continue
            # 对每一个字典r，提取所有字段到FirewallRule中
            src_ip=r.get("src_ip")
            dst_ip=r.get("dst_ip")
            proto=r.get("proto")
            src_port=r.get("src_port")
            dst_port=r.get("dst_port") 
            action=r.get("action","deny")   
            rule=FirewallRule(src_ip,dst_ip,proto,src_port,dst_port,action)
            # append them into rules
            rules.append(rule)

        return rules

    def install_rules(self, ofctls):
        """
        Install firewall rules to all switches.
        """
        # 对每一台交换机dpid 都进行每一条规则的install
        for dpid, ofctl in ofctls.items():
            for rule in self.rules:
                action = rule.action
                
                # only handle deny rules
                if action is not None and action!="deny":
                    continue

                src_ip = self._normalize_any(rule.src_ip) or 0
                dst_ip = self._normalize_any(rule.dst_ip) or 0
                # convert protocol name to protocol number
                proto_number=self._proto_to_number(rule.proto)

                # normalize source and destination ports
                try:
                    src_port=self._normalize_port(rule.src_port)
                    dst_port=self._normalize_port(rule.dst_port)
                except(ValueError,TypeError):
                    continue # 跳过不合法端口号
                # TODO: skip invalid port rules 什么是invalid?? 超范围
                if src_port<0 or src_port>65535:
                    continue
                if dst_port<0 or dst_port>65535:
                    continue

                # avoid duplicated flow installation
                key=(dpid,src_ip,dst_ip,proto_number,src_port,dst_port,action)
                if key in self.installed:
                    continue
                # use ofctl.set_flow() to install a high-priority drop flow
                # set_flow() 向当前交换机下发一条流表规则
                ofctl.set_flow(
                    cookie=self.COOKIE,
                    priority=self.PRIORITY,
                    dl_type=ether.ETH_TYPE_IP, #?
                    nw_src=src_ip,
                    nw_dst=dst_ip,
                    nw_proto=proto_number,
                    tp_src=src_port, #?
                    tp_dst=dst_port,
                    actions=[] # 空列表就表示firewall drop
                )

                self.installed.add(key)

                