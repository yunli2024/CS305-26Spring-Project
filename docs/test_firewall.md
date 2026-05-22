# Firewall Test Steps

本文档用于在切换到 Linux/Mininet 环境后测试 `feature_c_firewall` 分支中的 firewall 功能。

## 1. Windows 侧提交前检查

当前可以先把代码 commit/push 到 GitHub，再重启进入 Linux 测试。提交前建议确认当前分支：

```powershell
git status --short --branch
```

确认在：

```text
feature_c_firewall
```

建议只 stage 需要提交的文件，不要把 `__pycache__/` 加进去：

```powershell
git add controller.py firewall.py AGENT.md docs\firewall.md docs\test_firewall.md
git commit -m "Implement firewall rule installation"
git push origin feature_c_firewall
```

如果 `git status` 里还有 `__pycache__/`，不要提交它。之后可以手动处理或添加 `.gitignore`，但不要使用批量删除命令。

## 2. Linux 侧拉取代码

进入 Linux 后，进入项目目录：

```bash
cd /path/to/CS305-26Spring-Project
```

切换到 firewall 分支并拉取最新代码：

```bash
git fetch --all --prune
git checkout feature_c_firewall
git pull
```

确认当前分支和状态：

```bash
git status --short --branch
```

## 3. 环境检查

激活课程 Python 环境：

```bash
conda activate cs305
```

确认 `osken-manager` 可用：

```bash
osken-manager --version
```

确认 Mininet 可用：

```bash
sudo mn --test pingall
```

确认 `arping` 可用：

```bash
arping
```

如果提示 command not found：

```bash
sudo apt-get install arping
```

## 4. 清理旧 Mininet 状态

每次测试前建议清理旧拓扑：

```bash
sudo mn -c
```

## 5. 启动 Controller

打开第一个终端，在项目根目录启动 controller：

```bash
cd /path/to/CS305-26Spring-Project
conda activate cs305
osken-manager --observe-links controller.py
```

保持这个终端运行，不要关闭。

## 6. 运行 Firewall 测试

打开第二个终端：

```bash
cd /path/to/CS305-26Spring-Project/tests/firewall_test
conda activate cs305
sudo env "PATH=$PATH" python test_network.py
```

测试脚本会创建如下拓扑：

```text
h1 ---\
h2 ---- s1
h3 ---/
```

测试 IP：

- h1: `192.168.117.2/24`
- h2: `192.168.117.3/24`
- h3: `192.168.117.4/24`

规则文件 `firewall_rule.json` 预期阻止：

- h1 -> h2 ICMP
- h1 -> h2 TCP/80

## 7. 预期结果

测试脚本会执行四组检查。

### Test 1: h1 -> h2 ICMP

命令行为：

```bash
h1 ping -c 2 -W 1 192.168.117.3
```

预期：失败，出现 packet loss。

原因：`firewall_rule.json` 中 deny 了 `192.168.117.2 -> 192.168.117.3` 的 ICMP。

### Test 2: h1 -> h3 ICMP

命令行为：

```bash
h1 ping -c 2 -W 1 192.168.117.4
```

预期：成功，packet loss 为 0%。

原因：没有规则阻止 h1 到 h3。

### Test 3: h1 -> h2 TCP/80

命令行为：

```bash
curl http://192.168.117.3:80/
```

预期：失败或超时。

原因：`firewall_rule.json` 中 deny 了 `192.168.117.2 -> 192.168.117.3` 的 TCP destination port 80。

### Test 4: h1 -> h2 TCP/8080

命令行为：

```bash
curl http://192.168.117.3:8080/
```

预期：成功，能看到类似：

```text
HTTP_CODE=200
```

原因：规则只阻止 TCP/80，没有阻止 TCP/8080。

## 8. 查看 Flow Table

测试脚本最后会进入 Mininet CLI。可以在 CLI 中执行：

```bash
dpctl dump-flows
```

应能看到高优先级 firewall drop flow，重点检查：

- priority 高于普通 forwarding flow。
- 有匹配 `192.168.117.2 -> 192.168.117.3` 的 ICMP flow。
- 有匹配 `192.168.117.2 -> 192.168.117.3` 且 `tp_dst=80` 的 TCP flow。
- drop flow 的 actions 应为空。

如果 `dpctl dump-flows` 输出太乱，也可以在 Linux 终端中用 OVS 命令检查：

```bash
sudo ovs-ofctl -O OpenFlow10 dump-flows s1
```

## 9. 常见问题排查

### 规则没有生效

先检查 controller 终端是否有报错。如果 controller 报 `unexpected keyword argument 'action'`，说明 `ofctl.set_flow()` 参数应为 `actions=[]`。

再检查 `firewall_rule.json` 是否在项目根目录，并且文件名和 `Firewall()` 默认读取的文件名一致。

### 所有 ping 都失败

可能是 ARP 或 forwarding rules 没有正常工作。先在 Mininet CLI 中执行：

```bash
arping_all
pingall
dpctl dump-flows
```

如果没有普通 forwarding flow，需要检查 switching 部分是否正常安装目的 MAC 转发规则。

### h1 -> h2 TCP/80 没有被阻止

检查 TCP/80 规则是否被安装：

```bash
dpctl dump-flows
```

重点看是否有：

- `nw_src=192.168.117.2`
- `nw_dst=192.168.117.3`
- `nw_proto=6`
- `tp_dst=80`
- 空 actions

### h1 -> h2 TCP/8080 也被阻止

检查 TCP 规则是否错误地 wildcard 了目的端口。`dst_port=80` 应该被转换成 `tp_dst=80`，不能变成 `0`。

### 修改后重新测试

每次修改 controller 或 firewall 后：

1. 停止 Mininet CLI。
2. 停止 controller。
3. 清理 Mininet。
4. 重新启动 controller。
5. 重新运行测试脚本。

命令：

```bash
sudo mn -c
osken-manager --observe-links controller.py
cd ./tests/firewall_test/
sudo env "PATH=$PATH" python test_network.py
```

