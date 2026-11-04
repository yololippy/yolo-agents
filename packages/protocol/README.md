# Protocol

跨 Agent 与客户端的稳定通信契约。这里放 API 版本、JSON 模型和兼容性规则；具体的 Python Host、Swift SDK 和 Agent 业务实现不放在这里。

- [通信协议 v1](COMMUNICATION_V1.md)

v1 同时定义文本与图片消息：图片字节独立上传/鉴权下载，消息和事件只携带稳定的媒体元数据与 ID；Agent 私有数据仍按各自 Host 数据目录隔离。
