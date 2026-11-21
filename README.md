# PagerMaid-Pyro 插件集

适用于 PagerMaid-Pyro 的个人插件集合，涵盖 AI 助手、消息处理、订阅与网络工具、日常自动化等功能。每个插件均以独立的 Python 文件提供；本页按用途整理插件入口。

## 安装前准备

1. 部署并启动已完成 Telegram 登录配置的 [PagerMaid-Pyro](https://github.com/TeamPGM/PagerMaid-Pyro)；安装插件的命令由运行该实例的账号发送。
2. 确认插件需要的 Python 版本、Python 包、外部命令行工具及服务凭据。请先阅读所选插件源文件开头的说明和首次运行时显示的菜单；插件依赖应安装到运行 PagerMaid-Pyro 的同一 Python 环境。
3. 插件在 PagerMaid-Pyro 服务器进程中运行。需要 CLI 登录态的插件，应由启动机器人进程的同一系统用户完成登录。
4. [codexai.py](codexai.py) 明确要求 Python 3.9+ 和 Codex CLI；CLI 必须在 PagerMaid-Pyro 服务器上可用，并以运行机器人进程的系统用户登录。

## 安装与管理

### 方法一：PagerMaid-Pyro 内置安装器

适合安装本仓库的单个插件，不需要先安装 [pm.py](pm.py)。在 GitHub 打开所需的 `.py` 文件，下载源文件；在 Telegram 中将其作为**文件/文档**发送给 PagerMaid-Pyro 正在使用的账号，然后回复该文件发送内置安装命令：

```text
,apt install
```

内置安装器依据附件文件名将插件放入 PagerMaid-Pyro 的 `plugins` 目录并刷新已加载插件；请保留原始 `.py` 扩展名。也可把 `.py` 文件附在包含命令的同一条消息中。

### 方法二：使用本仓库的 `pm.py`

先用方法一安装 [pm.py](pm.py)，以后可用它安装和管理本地插件。将插件 `.py` 文件作为 Telegram 文档发送，再回复文件发送 `,az`：

```text
回复插件 .py 文件：,az
```

也可以把 `.py` 文件附在 `,az` 命令消息中。安装或覆盖后会自动刷新插件。常用命令如下：

| 命令 | 功能 |
| --- | --- |
| `,pm help` | 查看本插件的管理帮助。 |
| `,az` | 安装或覆盖附件/回复中的 `.py` 文件，并刷新插件。 |
| `,xz 插件名` | 卸载插件；例如 `,xz ai`。也可回复插件文件后只发送 `,xz`。 |
| `,sx` | 刷新全部插件。 |
| `,sc` | 回复一条插件命令的结果，上传生成该结果的插件源码。 |
| `,sc 命令名` | 按命令名上传对应插件源码。 |
| `,bf` | 将 `plugins` 目录中的 `.py` 插件打包并发送到 Telegram 收藏夹。 |

### 安装后检查与日常使用

1. 发送 PagerMaid-Pyro 内置命令 `,apt status`，查看插件的运行、禁用或加载失败状态；使用 `pm.py` 安装时，成功提示也会报告刷新结果。
2. 按下方目录找到插件的命令入口，在 Telegram 中发送相应命令。需要设置的插件通常会在首次运行时显示菜单；例如 `,ai help`、`,ck help`、`,h help` 和 `,pm help`。按该插件提示配置 API 地址、密钥、模型或其他选项。
3. 若命令没有响应，先查看 `,apt status`，确认文件名对应的插件已加载，再核对命令前缀、所需依赖、凭据及该插件的配置菜单。
4. 更新插件时重新下载 `.py` 文件并按相同方法安装；`pm.py` 的 `,az` 会覆盖并刷新。卸载用 `,xz 插件名`，或使用内置 `,apt remove 插件名`；安装后也可用 `,sx` 重新加载全部插件。

### 命令冲突

- [ai.py](ai.py)、[gp.py](gp.py) 与 [codexai.py](codexai.py) 都注册 `,ai`；同时只加载其中一个，避免命令重名。
- [minimax.py](minimax.py) 使用 `,m`；可与上述 Codex AI 插件共存。

安装、更新或卸载后，可用 `,apt status` 检查插件状态。

## 插件目录

### AI 助手与对话

| 插件 | 功能 |
| --- | --- |
| [ai.py](ai.py) | ChatGPT 日常问答助手，支持回复消息、图片输入、模型及推理强度选择，以及 Codex 额度查询。 |
| [api.py](api.py) | 查询星驰指定 API Key 的用量和余额，生成用量图卡。 |
| [codex.py](codex.py) | 查询 Codex 使用额度、额度窗口及可用重置次数。 |
| [codexai.py](codexai.py) | 使用已登录的 Codex CLI 提供 AI 对话，支持上下文、图片分析、图片生成及模型设置。 |
| [cz.py](cz.py) | 以图片展示 Codex 额度重置记录，默认查看最近有记录的月份，也可指定月份。 |
| [deepseek.py](deepseek.py) | 通过 DeepSeek 官方 API 问答，支持回复消息、模型与推理强度设置，以及余额查询。 |
| [gp.py](gp.py) | 通过 ChatGPT OAuth 登录提供问答，支持回复上下文、图片输入、模型与推理强度选择。 |
| [minimax.py](minimax.py) | MiniMax AI 助手，支持文字问答、回复内容分析、图片理解、联网查询和图片生成。 |
| [workbuddy.py](workbuddy.py) | 使用 `,wk` 调用自配 WorkBuddy 接口对话，使用 `,key` 查询 DeepSeek 总量或指定 Key 用量。 |
| [nq.py](nq.py) | 使用 Codex 提供群聊自动回复，可按全群或指定成员启用，并管理模型、推理强度及上下文。 |
| [zj.py](zj.py) | 使用 MiniMax 总结当前群聊当天的消息，整理话题与重点信息。 |

> [ai.py](ai.py)、[gp.py](gp.py) 和 [codexai.py](codexai.py) 共用 `,ai` 命令；请只加载其中一个，详见上方[命令冲突](#命令冲突)说明。

### 消息、群聊与内容处理

| 插件 | 功能 |
| --- | --- |
| [b.py](b.py) | 按北京时间范围导出当前群聊记录至收藏夹，支持纯文本或包含普通文件的 ZIP；时间范围与导出类型为全局设置。 |
| [bc.py](bc.py) | 保存或转存消息与媒体，支持回复消息、消息链接、批量与范围处理，以及保存媒体到服务器本地。 |
| [dme.py](dme.py) | 按全部或指定数量清理当前聊天中自己的消息，也支持清理收藏夹，显示清理进度。 |
| [ds.py](ds.py) | 使用 `,dingshi` 每天定时发送消息或执行插件命令，支持查看任务和按 ID 删除。 |
| [fanyi.py](fanyi.py) | 回复消息翻译为中文，优先使用已配置的 DeepL，失败时回退到 Google 翻译。 |
| [music.py](music.py) | 搜索歌曲并展示前七项结果，回复序号获取 FLAC 音乐下载链接。 |
| [t.py](t.py) | 回复 YAML 文件，将 `.yaml` / `.yml` 改为 `.txt` 后发送，保持文件内容不变。 |
| [xjj.py](xjj.py) | 随机获取短视频，优先由 Telegram 抓取；失败时尝试内存中转。 |

### 插件管理、账号与自动化

| 插件 | 功能 |
| --- | --- |
| [cj.py](cj.py) | 以图卡展示已安装插件数量和进程运行时间，列出功能说明并支持自定义备注。 |
| [ck.py](ck.py) | Telegram 机器人每日自动签到，支持固定时间、顺序点击、候选按钮匹配及结果推送。 |
| [git.py](git.py) | 监控 GitHub Releases，管理订阅仓库，并通过 Telegram Bot 推送更新。 |
| [pm.py](pm.py) | 安装、覆盖、卸载、刷新插件，上传源码及备份全部插件。 |
| [pmcaptcha.py](pmcaptcha.py) | 私聊防骚扰验证，提供验证码、白名单及多种验证规则。 |
| [reload.py](reload.py) | 本地插件热刷新，配合 VPS 上的文件刷新工具使用。 |
| [sj.py](sj.py) | 在昵称中显示时间与天气，支持通过菜单手动设置地区，地区配置仅保存在本机。 |
| [submanger.py](submanger.py) | 管理 Telegram 订阅，支持添加、查询、更新、删除、测活、仓库管理及数据库备份恢复。 |

### 网络、订阅与查询工具

| 插件 | 功能 |
| --- | --- |
| [c.py](c.py) | 查询机场订阅的名称、已用与剩余流量、总量及到期时间，支持回复消息和多个链接。 |
| [dc.py](dc.py) | 测试服务器到 Telegram DC1–DC5 的网络延迟，并显示账号所属 DC。 |
| [huilv.py](huilv.py) | 查询法币与加密货币汇率、换算金额，支持货币代码及中文名称、国家名称等输入。 |
| [ip.py](ip.py) | 查询 IP 或域名的地理位置、ISP、组织、ASN 与路由信息。 |
| [jg.py](jg.py) | 分别查询 ChatGPT Plus 月付直充与 Claude Pro 报价，按价格升序展示前 10 家店铺。 |
| [dy.py](dy.py) | 查询 App 订阅及内购项目最便宜的 10 个地区，支持按项目序号选择。 |
| [lianjie.py](lianjie.py) | 将代理节点或节点配置转换为 Mihomo YAML 订阅，并上传生成短链接。 |
| [LL.py](LL.py) | 调用指定 Telegram Bot 分析 URL、执行 Ping 测试，并转发图片结果。 |
| [speedtest.py](speedtest.py) | 使用 Ookla 测试服务器上传、下载与延迟，支持自动选择或指定服务器，并输出测速图卡。 |
| [zhuanhuan.py](zhuanhuan.py) | 将节点或订阅链接转换为 Clash 与 Surge 可用的订阅链接。 |
| [zt.py](zt.py) | 查看 CPU、内存、插件数量、运行时间、Telegram DC 及网络延迟等运行状态。 |

## 许可

本仓库及各插件的许可信息见 [LICENSE](LICENSE)；文件内嵌的第三方字体保留其原有许可声明。
