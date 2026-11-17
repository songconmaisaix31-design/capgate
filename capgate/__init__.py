"""capgate —— 本地能力网关。

控制面：路由 / 策略 / 状态 / 账本
能力池：MCP / RAG / 文档 / SDK（共享、可复用、版本钉死）
领域层：env（某业务域的能力组合 + 自带 agent/model + 自带性能画像）
编排层：一个任务拉多个 env，env 之间不直连，只经 artifact store 交换产物
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
