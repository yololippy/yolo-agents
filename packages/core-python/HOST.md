# Mac 常驻聊天后端

默认演示模式使用 Python 标准库，不需要安装 Python 第三方包。PydanticAI 真实模型模式的依赖和配置由 [运行时说明](README.md) 说明；没有配置和验证前，演示模式会明确显示为 demo。

本仓库代码依赖已安装到隔离的 `.venv`，完整版本快照见 `requirements.lock`，`pip check` 已通过。重新配置同一开发基线时可执行：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r packages/core-python/requirements.lock
```

在仓库根目录运行：

```sh
PYTHONPATH=packages/core-python python3 -m yolo_agents --mode demo
```

默认地址 `http://127.0.0.1:8765`，数据保存到 `~/Library/Application Support/YoloAgents`。启动控制台显示 Mac / iOS 两个一次性配对码，十分钟过期；客户端按自身平台使用对应配对码。不要把配对码、设备 token、模型 API key 提交仓库或发到共享日志。

本地协议验证可以指定临时或单独的数据目录和端口：

```sh
PYTHONPATH=packages/core-python python3 -m yolo_agents --mode demo --data-dir /tmp/yolo-agents-demo --port 8765
```

关闭客户端窗口不会停止独立运行的后端。终端 Ctrl-C / SIGTERM 关闭服务；相同数据目录只允许一个服务实例。重启保留会话、消息、事件和设备授权；Runtime 已保存的完整回复可补齐产品结果，其余未完成回复显示中断，不自动重新执行模型或工具。

iPhone 真机访问需提供可达的 Mac HTTPS 地址、受信任证书及私钥。例如：

```sh
PYTHONPATH=packages/core-python python3 -m yolo_agents --mode demo --host 0.0.0.0 \
  --tls-cert /path/to/server-certificate.pem --tls-key /path/to/server-private.key
```

脚本拒绝在未配置 TLS 时监听非本机地址。客户端仍需正常验证服务端证书，不能关闭证书校验。当前不自动安装远程网络软件或配置公网端口。

设备撤销、消息幂等和同步细节见 [通信协议](../protocol/COMMUNICATION_V1.md)。iPhone 首版仅聊天与查看；交易执行、确认和预授权接口均返回 403。

交易 Agent 的 IBKR 模拟盘 / 正式市场配置、健康状态和启动命令见 [交易 Agent 说明](../../agents/trading-agent/README.md)。共享 Host 不接受券商专属参数。

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=packages/core-python python3 -m unittest discover -s packages/core-python/tests -v
```

当前交付是聊天后端及协议。IBKR、交易审核/执行、定时订单和通知通道没有因本服务可运行而自动完成。
