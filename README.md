# PagerMaid Plugins

个人维护的 [PagerMaid-Pyro](https://github.com/TeamPGM/PagerMaid-Pyro) 插件集合，涵盖 AI 助手、消息管理、订阅工具、网络诊断与自动化。

## 插件目录

| 插件 | 功能简介 |
| :--- | :--- |
| [`b.py`](b.py) | 按北京时间范围导出当前群聊记录至收藏夹，支持纯文本或包含普通文件的 ZIP；时间范围与导出类型为全局设置。 |
| [`bc.py`](bc.py) | 保存或转存消息与媒体，支持回复消息、消息链接、批量与范围处理，以及保存媒体到服务器本地。 |
| [`c.py`](c.py) | 查询机场订阅的名称、已用与剩余流量、总量及到期时间，支持回复消息和多个链接。 |
| [`cj.py`](cj.py) | 列出已安装插件及功能说明，支持自定义备注。 |
| [`ck.py`](ck.py) | 管理 Telegram Bot 签到任务，支持固定或随机时间、按钮签到及结果推送。 |
| [`codex.py`](codex.py) | 查询 Codex 使用额度、额度窗口及可用重置次数。 |
| [`codexai.py`](codexai.py) | 基于已登录的 Codex CLI 提供 AI 对话，支持上下文、图片分析、图片生成及模型设置。 |
| [`cz.py`](cz.py) | 查询第三方数据源提供的全部 Codex 额度重置记录，按月份区分普通与储备重置，较早记录折叠展示。 |
| [`dc.py`](dc.py) | 测试服务器到 Telegram DC1–DC5 的网络延迟，并显示账号所属 DC。 |
| [`dme.py`](dme.py) | 清理当前聊天中自己发送的历史消息，支持全部清理或指定最新消息数量，并显示处理进度。 |
| [`ds.py`](ds.py) | 每天定时向指定聊天发送内容；发送插件命令时可在本地分派执行，支持跨聊天查看任务和按 ID 删除。 |
| [`fanyi.py`](fanyi.py) | 回复消息翻译为中文；优先使用已配置的 DeepL，失败时回退至 Google 翻译。 |
| [`git.py`](git.py) | 监控 GitHub Releases，管理订阅仓库，并通过 Telegram Bot 推送更新。 |
| [`gp.py`](gp.py) | 通过 ChatGPT OAuth 登录提供问答，支持回复上下文、图片输入、模型与推理强度选择。 |
| [`huilv.py`](huilv.py) | 查询法币与加密货币汇率、换算金额，支持货币代码及中文名称、国家名称等输入。 |
| [`ip.py`](ip.py) | 查询 IP 或域名的地理位置、ISP、组织、ASN 与路由信息。 |
| [`lianjie.py`](lianjie.py) | 将代理节点或节点配置转换为 Mihomo YAML 订阅，并上传生成短链接。 |
| [`LL.py`](LL.py) | 调用指定 Telegram Bot 分析 URL、执行 Ping 测试，并转发图片结果。 |
| [`minimax.py`](minimax.py) | MiniMax AI 助手，支持文字问答、回复内容分析、图片理解、联网查询和图片生成。 |
| [`nq.py`](nq.py) | 基于 Codex 的群聊自动回复，可按全群或指定成员启用，并管理模型、推理强度及上下文。 |
| [`pm.py`](pm.py) | 安装、覆盖、卸载、刷新插件，上传源码及备份全部插件。 |
| [`pmcaptcha.py`](pmcaptcha.py) | 私聊防骚扰验证，提供验证码、白名单及多种验证规则。 |
| [`speedtest.py`](speedtest.py) | 使用 Ookla 测试服务器上传、下载与延迟，支持自动选择、指定及查询附近测速服务器。 |
| [`submanger.py`](submanger.py) | 管理 Telegram 订阅，支持添加、查询、更新、删除、测活、仓库管理及数据库备份恢复。 |
| [`t.py`](t.py) | 回复 YAML 文件，将 `.yaml` / `.yml` 改为 `.txt` 后发送，保持文件内容不变。 |
| [`xjj.py`](xjj.py) | 随机获取短视频，优先由 Telegram 抓取；失败时尝试内存中转。 |
| [`zhuanhuan.py`](zhuanhuan.py) | 将节点或订阅链接转换为 Clash 与 Surge 可用的订阅链接。 |
| [`zj.py`](zj.py) | 使用 MiniMax 总结当前群聊当天的消息，整理话题与重点信息。 |
| [`zt.py`](zt.py) | 查看 CPU、内存、插件数量、运行时间、Telegram DC 及网络延迟等运行状态。 |
