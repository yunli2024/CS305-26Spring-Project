# CS305 Demo Defense Guide

This document is a complete playbook for the on-site project defense. It is written from the perspective of the actual demo flow required by `project-instructions.md`, and it includes:

- what the graders are checking,
- what code files you should mention,
- the exact commands to run,
- what outputs to show,
- what to say during each step,
- the complex topology and bonus items.

Use this document as the single reference during final rehearsal and on the demo day.

---

## 1. What The Graders Will Check

According to `project-instructions.md`, the demo mainly covers three required parts:

1. DHCP
2. Shortest path switching
3. Firewall

In addition, you may demonstrate bonus parts if time allows:

- DNS
- TCP congestion
- Bufferbloat

The most important point is that the graders are checking **behavior**, not only code structure. That means:

- the controller must start correctly,
- Mininet topologies must run correctly,
- the controller console should show useful path or event information,
- connectivity and blocking behavior must match the expected design.

---

## 2. High-Level Project Understanding

You should understand the project in one sentence:

> Mininet builds the virtual network, and the os-ken controller decides how the network behaves.

### Runtime Roles

- `Mininet`: creates hosts, switches, and links.
- `controller.py`: receives topology and packet events and installs OpenFlow rules.
- `dhcp.py`: handles DHCP address allocation.
- `firewall.py`: installs high-priority drop rules.
- `tests/...`: starts specific topologies and verifies behavior.

### Core Files To Mention

- `controller.py`
- `dhcp.py`
- `firewall.py`
- `firewall_rule.json`
- `ofctl_utilis.py`
- `tests/dhcp_test/`
- `tests/switching_test/`
- `tests/firewall_test/`
- `docs/complex-case-topology.md`

---

## 3. Demo Environment Setup

Open **two terminals**.

### Terminal 1: controller

```bash
cd /home/w1369/computer_network/make_proj/CS305-26Spring-Project
sudo mn -c
/home/w1369/miniforge3/envs/cs305/bin/osken-manager --observe-links controller.py
```

This terminal is for:

- controller startup,
- path printouts,
- topology reaction,
- firewall rule installation behavior,
- DHCP log behavior.

### Terminal 2: Mininet test scripts

All test scripts are started here.

Before switching to a different topology, run:

```bash
sudo mn -c
```

---

## 4. Suggested Demo Order

Use this order in the defense:

1. Project introduction
2. DHCP basic
3. DHCP custom configuration
4. DHCP pool exhaustion
5. Shortest path basic
6. Shortest path complex case
7. Firewall basic
8. Firewall complex case
9. Bonus DNS
10. Bonus TCP congestion
11. Bonus Bufferbloat

This order follows the project instruction structure and keeps the required parts before the bonus parts.

---

## 5. Opening Script

You can say this at the beginning:

> This project implements a centralized SDN controller using os-ken and Mininet.  
> Mininet is used to build the virtual network, while the controller handles DHCP, shortest-path forwarding, and firewall rule installation.  
> I will demonstrate the project in the same order as the project instructions: DHCP, shortest path switching, firewall, and then the bonus experiments.

---

## 6. DHCP Demo

## 6.1 Goal

The instruction requires three DHCP demonstrations:

1. basic DHCP allocation with default configuration,
2. DHCP allocation after changing `start_ip`, `end_ip`, and `netmask`,
3. address pool exhaustion where only the first `n` hosts receive addresses.

### Main code to mention

- `dhcp.py`
- `controller.py`
- `tests/dhcp_test/test_network.py`
- `tests/dhcp_test/test_custom_config.py`
- `tests/dhcp_test/test_small_pool_exhaustion.py`

### What to say before the demo

> The DHCP module is implemented in `dhcp.py`.  
> It handles DISCOVER, OFFER, REQUEST, and ACK, and it keeps track of pending offers, confirmed leases, lease expiration, and pool exhaustion.

---

## 6.2 DHCP Basic Test

### Command

Terminal 2:

```bash
sudo env "PATH=$PATH" /home/w1369/miniforge3/envs/cs305/bin/python tests/dhcp_test/test_network.py
```

Inside Mininet CLI:

```text
h1 ifconfig h1-eth0
h2 ifconfig h2-eth0
h1 ping -c 2 192.168.1.3
exit
```

### What to show

- `h1` gets a valid IP
- `h2` gets a valid IP
- `h1` can ping `h2`

### What to say

> This is the basic DHCP test from the original repository.  
> The hosts are created without predefined IP addresses, and the controller assigns valid addresses dynamically.  
> After the allocation, the two hosts can communicate normally.

---

## 6.3 DHCP Custom Configuration Test

This demonstrates instruction item 2 for DHCP.

### Restart controller with custom DHCP settings

Terminal 1:

```bash
sudo mn -c
DHCP_START_IP=10.10.0.10 DHCP_END_IP=10.10.0.12 DHCP_NETMASK=255.255.255.248 /home/w1369/miniforge3/envs/cs305/bin/osken-manager --observe-links controller.py
```

### Command

Terminal 2:

```bash
sudo env "PATH=$PATH" DHCP_START_IP=10.10.0.10 DHCP_END_IP=10.10.0.12 DHCP_NETMASK=255.255.255.248 /home/w1369/miniforge3/envs/cs305/bin/python tests/dhcp_test/test_custom_config.py
```

### What to show

- assigned IPs are in the range `10.10.0.10` to `10.10.0.12`
- subnet information changes with the new netmask
- communication still works

### What to say

> This test changes the DHCP address pool and netmask through environment variables.  
> It shows that the server does not depend on a fixed address range and can correctly allocate addresses under a new configuration.

---

## 6.4 DHCP Pool Exhaustion Test

This demonstrates instruction item 3 for DHCP.

### Keep the same custom-config controller

### Command

Terminal 2:

```bash
sudo mn -c
sudo env "PATH=$PATH" DHCP_START_IP=10.10.0.10 DHCP_END_IP=10.10.0.12 DHCP_NETMASK=255.255.255.248 /home/w1369/miniforge3/envs/cs305/bin/python tests/dhcp_test/test_small_pool_exhaustion.py
```

### What to show

- only the first 3 hosts receive valid addresses
- the remaining hosts do not receive any address

### What to say

> In this case the pool size is smaller than the number of hosts.  
> The first hosts get valid addresses, while the remaining hosts stay unconfigured, which matches the required behavior in the instruction.

---

## 6.5 Optional Extra DHCP Tests

These are not the core instruction items, but they are useful if the graders ask deeper questions.

### OFFER timeout reclaim

```bash
sudo mn -c
/home/w1369/miniforge3/envs/cs305/bin/osken-manager --observe-links controller.py
sudo env "PATH=$PATH" /home/w1369/miniforge3/envs/cs305/bin/python tests/dhcp_test/test_offer_timeout.py
```

### Concurrent allocation and default pool exhaustion

```bash
sudo mn -c
sudo env "PATH=$PATH" /home/w1369/miniforge3/envs/cs305/bin/python tests/dhcp_test/test_concurrent_exhaust.py
```

### Lease expiration and renewal

```bash
sudo mn -c
sudo env "PATH=$PATH" /home/w1369/miniforge3/envs/cs305/bin/python tests/dhcp_test/test_lease_expiry.py
```

### One-line explanation

> We also implemented bonus DHCP behaviors including timeout reclaim, concurrent allocation, lease expiration, and renewal.

---

## 7. Shortest Path Switching Demo

## 7.1 Goal

The instruction requires:

1. basic switching correctness,
2. a complex topology with more than 6 hosts, more than 6 switches, and more than 10 edges,
3. dynamic modification through Mininet CLI,
4. printing topology/path information in the controller console.

### Main code to mention

- `controller.py`
- `ofctl_utilis.py`
- `tests/switching_test/test_network.py`
- `tests/switching_test/complex_topology_demo.py`
- `docs/complex-case-topology.md`

### What to say before the demo

> The switching logic is implemented in `controller.py`.  
> The controller learns hosts through ARP, learns topology through switch and link events, computes shortest paths, and installs destination-MAC forwarding rules on the switches.

---

## 7.2 Switching Basic Test

### Command

Terminal 1 should use the default controller:

```bash
sudo mn -c
/home/w1369/miniforge3/envs/cs305/bin/osken-manager --observe-links controller.py
```

Terminal 2:

```bash
sudo env "PATH=$PATH" /home/w1369/miniforge3/envs/cs305/bin/python tests/switching_test/test_network.py
```

Inside Mininet CLI:

```text
pingall
exit
```

### What to show

- all three hosts are reachable
- controller terminal prints shortest path information

### What to say

> This is the baseline triangle topology.  
> After host and link discovery, the controller installs forwarding rules so that all hosts can reach each other using shortest paths.

---

## 7.3 Complex Topology Figure

Before running the complex case, open:

- [complex-case-topology.md](/home/w1369/computer_network/make_proj/CS305-26Spring-Project/docs/complex-case-topology.md)

This document contains:

- the Mermaid topology figure,
- the host and switch counts,
- the number of inter-switch edges,
- the expected shortest paths between hosts,
- the operations used in the demo.

### What to say

> This is our own complex topology.  
> It contains 7 hosts, 7 switches, and 12 inter-switch edges, so it satisfies the instruction requirement of more than 6 hosts, more than 6 switches, and more than 10 edges.  
> We also prepared the expected shortest paths between host pairs for comparison.

---

## 7.4 Complex Switching Case

### Command

Terminal 2:

```bash
MININET_CLI=1 sudo env "PATH=$PATH" /home/w1369/miniforge3/envs/cs305/bin/python tests/switching_test/complex_topology_demo.py
```
Use complex_topology_manual.py
  sudo env "PATH=$PATH" /home/w1369/miniforge3/envs/cs305/bin/python tests/switching_test/complex_topology_manual.py  

### Recommended CLI sequence

Inside Mininet CLI:

```text
pingall
net
link s2 s5 down
pingall
link s2 s5 up
pingall
switch s6 stop
switch s6 start
pingall
sh ovs-ofctl mod-port s4 4 down
pingall
sh ovs-ofctl mod-port s4 4 up
pingall
arping_all
exit
```

### What each operation demonstrates

- `pingall`: initial connectivity
- `link s2 s5 down`: link deletion behavior
- `link s2 s5 up`: link addition behavior
- `switch s6 stop`: switch deletion behavior
- `switch s6 start`: switch addition behavior
- `mod-port ... down/up`: port modification behavior
- `arping_all`: host discovery refresh

### What to show

- `pingall` stays connected after topology changes
- controller terminal updates shortest paths
- controller terminal reflects topology adaptation

### What to say

> This complex case demonstrates dynamic topology adaptation.  
> When a link, switch, or port changes, the controller refreshes the topology view and updates the forwarding rules so that host connectivity is preserved through alternative shortest paths whenever possible.

### Note about event coverage

From a demo perspective, the behavior works correctly for:

- `handle_host_add`
- `handle_switch_add`
- `handle_switch_delete`
- `handle_link_add`
- `handle_link_delete`
- `handle_port_modify`

In the current implementation, effective link-down handling is achieved mainly through port-state changes and topology resynchronization, which still produces the correct demo behavior required by the instruction.

---

## 8. Firewall Demo

## 8.1 Goal

The instruction requires:

1. passing the basic firewall test,
2. reusing the complex switching topology,
3. demonstrating that firewall rules can make previously reachable hosts become unreachable.

### Main code to mention

- `firewall.py`
- `firewall_rule.json`
- `controller.py`
- `tests/firewall_test/test_network.py`
- `tests/firewall_test/firewall_complex_topology_demo.py`

### What to say before the demo

> The firewall is implemented by translating JSON policy rules into high-priority OpenFlow drop entries.  
> These entries are installed on the switches, so blocking happens inside the network rather than on the end hosts.

---

## 8.2 Firewall Basic Test

### Command

Terminal 2:

```bash
sudo env "PATH=$PATH" /home/w1369/miniforge3/envs/cs305/bin/python tests/firewall_test/test_network.py
```

### What to show

The script already performs the key checks automatically:

- `h1 -> h2` ICMP should fail
- `h1 -> h3` ICMP should pass
- `h1 -> h2:80` TCP should fail
- `h1 -> h2:8080` TCP should pass

### What to say

> This basic test shows that matching traffic is blocked while unrelated traffic is still allowed.  
> The behavior is policy-driven and is enforced by switch flow entries rather than host-side filtering.

---

## 8.3 Firewall Complex Case

### Command

Terminal 2:

```bash
MININET_CLI=1 sudo env "PATH=$PATH" /home/w1369/miniforge3/envs/cs305/bin/python tests/firewall_test/firewall_complex_topology_demo.py
```

### What to show

The script checks:

- `h1 -> h2` ICMP is blocked
- `h1 -> h2:80` is blocked
- `h1 -> h2:8080` is allowed
- unrelated traffic remains reachable
- reverse-direction traffic remains allowed
- firewall flow entries are visible on switches

### What to say

> This is the firewall version of the complex topology.  
> It reuses the same topology structure as the switching complex case.  
> The key result is that two hosts that were reachable in the plain switching case become unreachable after the firewall policy is installed, while other traffic still works.

---

## 9. Bonus Demonstrations

These are optional in the defense, but they help show completeness and extra effort.

---

## 9.1 Bonus DNS

### Command

Terminal 1:

```bash
sudo mn -c
/home/w1369/miniforge3/envs/cs305/bin/osken-manager --observe-links controller.py
```

Terminal 2:

```bash
sudo env "PATH=$PATH" /home/w1369/miniforge3/envs/cs305/bin/python tests/dns_test/test_network.py
```

### What to show

- the script prints a summary such as `4/4 checks passed`
- DNS queries are answered by the controller-hosted DNS server

### What to say

> We extended the controller with a DNS service, so the controller can also answer DNS traffic in addition to DHCP and forwarding logic.

---

## 9.2 Bonus TCP Congestion

### Command

No controller is needed for this script.

```bash
sudo mn -c
sudo env "PATH=$PATH" /home/w1369/miniforge3/envs/cs305/bin/python tests/bonus5_tcp_congestion/test_tcp_congestion.py
```

### What to show

- single-flow throughput results
- fairness results between Reno and Cubic
- the printed summary table

### What to say

> This bonus experiment studies TCP congestion behavior.  
> We compare throughput and fairness between different TCP algorithms under the same network bottleneck.

### Note

Warnings such as `sch_htb: quantum ... is big` are Linux traffic-control warnings and do not mean the experiment failed.

---

## 9.3 Bonus Bufferbloat

### Command

No controller is needed for this script.

```bash
sudo mn -c
sudo env "PATH=$PATH" /home/w1369/miniforge3/envs/cs305/bin/python tests/bonus5_bufferbloat/test_bufferbloat.py
```

### What to show

- throughput
- idle RTT
- busy RTT
- packet loss
- comparison between small queue and large queue

### What to say

> This experiment demonstrates bufferbloat.  
> With a larger queue, throughput may improve and loss may decrease, but the busy RTT increases dramatically, which shows the queueing delay problem clearly.

---

## 10. Full Test Coverage Summary

The following required and bonus scripts are now available:

### DHCP

- `tests/dhcp_test/test_network.py`
- `tests/dhcp_test/test_custom_config.py`
- `tests/dhcp_test/test_small_pool_exhaustion.py`
- `tests/dhcp_test/test_offer_timeout.py`
- `tests/dhcp_test/test_concurrent_exhaust.py`
- `tests/dhcp_test/test_lease_expiry.py`

### Switching

- `tests/switching_test/test_network.py`
- `tests/switching_test/complex_topology.py`
- `tests/switching_test/complex_topology_demo.py`

### Firewall

- `tests/firewall_test/test_network.py`
- `tests/firewall_test/firewall_complex_test.py`
- `tests/firewall_test/firewall_complex_topology_demo.py`

### Bonus

- `tests/dns_test/test_network.py`
- `tests/bonus5_tcp_congestion/test_tcp_congestion.py`
- `tests/bonus5_bufferbloat/test_bufferbloat.py`

### Automated regression script

- `run_all_tests.sh`

This script covers the main required tests and the bonus scripts currently integrated into the project.

---

## 11. Fast Rehearsal Version

If time is limited before the defense, rehearse only this sequence:

1. DHCP basic
2. DHCP custom config
3. DHCP pool exhaustion
4. Switching basic
5. Complex switching topology
6. Firewall basic
7. Complex firewall topology
8. DNS bonus
9. TCP congestion summary
10. Bufferbloat summary

If time is very short during the actual defense, the minimum safe set is:

1. DHCP basic
2. DHCP custom config or pool exhaustion
3. Switching basic
4. Complex switching case
5. Firewall basic
6. Firewall complex case

---

## 12. Final Closing Script

You can end the demo with:

> In summary, our controller supports DHCP allocation, shortest-path forwarding, and firewall policy enforcement.  
> We also prepared a complex topology with dynamic CLI operations and several bonus experiments including DNS, TCP congestion, and bufferbloat.  
> All major functionalities have corresponding Mininet test scripts and can be reproduced directly.

---

## 13. Related Files

- Instruction source: `project-instructions.md`
- Complex topology figure and paths: `docs/complex-case-topology.md`
- Team report source: `report/project_report.tex`
- Team report PDF: `report/project_report.pdf`

