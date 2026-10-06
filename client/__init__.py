"""client 包：底层网络通信层。

职责边界（务必守住，否则代码很快会烂掉）：
- 只管「怎么把字节发出去、怎么把字节收回来」：Session 管理、Header 拼装、超时与重试、异常翻译。
- **不解析业务语义**。登录成功与否、字段含义，交给 tasks/ 与 models/ 判断。

约定：包外层不做事，所有导入都写全路径，例如
    from client.session import GameSession
这样你一眼就能看出某个名字来自哪个文件。
"""

__all__: list[str] = []
