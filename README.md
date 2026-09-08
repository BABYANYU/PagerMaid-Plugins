# PagerMaid Plugins

适用于 [PagerMaid-Pyro](https://github.com/TeamPGM/PagerMaid-Pyro) 的个人插件合集。

## 插件列表

| 插件 | 指令 | 功能 |
| --- | --- | --- |
| [ai.py](./ai.py) | `,ai` | Codex AI 助手，支持文字、图片、联网查询、模型及推理强度切换。 |
| [bc.py](./bc.py) | `,bc` | 保存或转存 Telegram 消息与媒体，支持收藏夹、指定对话及 VPS 本地。 |
| [c.py](./c.py) | `,c` | 查询订阅链接信息。 |
| [cj.py](./cj.py) | `,cj` | 列出已安装的 PagerMaid 插件及功能说明。 |
| [ck.py](./ck.py) | `,ck` | 管理并定时执行 Telegram 机器人签到任务。 |
| [codex.py](./codex.py) | `,gpt`、`codex reset` | 查询 Codex 使用额度与重置卡，并支持手动重置。 |
| [dc.py](./dc.py) | `,dc` | 测试 VPS 到 Telegram 各数据中心的延迟，并显示账号会话所在 DC。 |
| [dme.py](./dme.py) | `,dme` | 批量删除自己发送的消息。 |
| [ds.py](./ds.py) | `,ds` | 每天在指定时间自动发送消息。 |
| [fanyi.py](./fanyi.py) | `,f` | 使用 DeepL 或备用翻译服务将消息翻译成中文。 |
| [huilv.py](./huilv.py) | `,h` | 查询加密货币和法币汇率，并进行数量换算。 |
| [ip.py](./ip.py) | `,ip` | 查询 IP、域名及 BGP 路由信息。 |
| [lianjie.py](./lianjie.py) | `,lj` | 将节点内容或配置文件转换为 Mihomo 订阅链接。 |
| [LL.py](./LL.py) | `,/analyzeurl`、`,/pingurl`、`,ll` | 在 Telegram 聊天窗口调用测速 Bot 并回传结果。 |
| [pm.py](./pm.py) | `,pm` | 安装、卸载、刷新、上传和备份 PagerMaid 插件。 |
| [pmcaptcha.py](./pmcaptcha.py) | `,pmcaptcha` | 为陌生人私聊提供验证码保护。 |
| [speedtest.py](./speedtest.py) | `,st` | 使用 Speedtest by Ookla 测试服务器网络。 |
| [submanger.py](./submanger.py) | `,s` | 管理、切换、检测、上传和下载订阅链接。 |
| [t.py](./t.py) | `,t` | 将 YAML 配置文件转换为 TXT 文件。 |
| [zhuanhuan.py](./zhuanhuan.py) | `,z` | 同时生成 Clash 与 Surge 订阅链接。 |

## 安装

下载需要的 `.py` 文件并放入 PagerMaid 的 `workdir/plugins/` 目录，然后执行：

```text
,reload
```
