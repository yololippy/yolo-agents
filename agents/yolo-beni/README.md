# YoloBeni Runtime

YoloBeni 私有的生活助手 Runtime。它复用共享 Host、PydanticAI/Harness 和 ChatGPT Provider，但使用自己的产品身份、授权、数据目录、记忆与 Skills。没有 IBKR、订单工具或 YoloTrade 私有依赖。

## 启动

从仓库根目录启动独立 ChatGPT Provider：

```sh
PYTHONPATH=packages/core-python:agents/yolo-beni/runtime-python .venv/bin/python -m yolo_beni --mode chatgpt
```

默认端口为 8766，数据目录为：

```text
~/Library/Application Support/YoloAgents/yolo-beni/
```

无模型调用的隔离演示：

```sh
PYTHONPATH=packages/core-python:agents/yolo-beni/runtime-python \
  .venv/bin/python -m yolo_beni --mode demo
```

依赖见 `runtime-python/requirements.txt`，精确版本继承共享 lock 文件。

## 产品边界

Mac 和 iOS App 配对同一个 YoloBeni Host 后共享聊天与记忆；OAuth 凭据只保留在 Mac 后端。日历、提醒事项、地图和健康等能力在首次接入时属于 YoloBeni 私有层，只有出现真实跨产品复用需求后才考虑抽取。

客户端见 [YoloBeni App](../../apps/yolo-beni/README.md)，完整隔离规则见 [架构](../../docs/ARCHITECTURE.md#身份授权与数据隔离)。

## 测试

```sh
PYTHONPATH=packages/core-python:agents/yolo-beni/runtime-python \
  .venv/bin/python -m unittest discover \
  -s agents/yolo-beni/runtime-python/tests -v
```

当前状态和后续生活能力只维护在 [ROADMAP](../../docs/ROADMAP.md)。
