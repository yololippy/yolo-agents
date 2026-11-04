# YoloBeni Python 运行时

入口：`yolo_beni`。默认以自己的名称 `YoloBeni`、端口 `8766` 和独立数据目录运行 ChatGPT Provider。只复用共享 Host，不导入 YoloTrade 或券商依赖。

从仓库根目录验证：

```sh
PYTHONPATH=packages/core-python:agents/yolo-beni/runtime-python .venv/bin/python -m unittest discover -s agents/yolo-beni/runtime-python/tests
```

启动方式与数据边界见 [YoloBeni 说明](../README.md)。
