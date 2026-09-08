import re
from urllib import parse

from pyrogram.types import Message

from pagermaid.listener import listener
from pagermaid.utils import alias_command

# 后端转换服务地址和配置模板链接
backend = "amyconvert.com/"
config = "https://raw.githubusercontent.com/ACL4SSR/ACL4SSR/master/Clash/config/ACL4SSR_Online_Mini_NoAuto.ini"

@listener(is_plugin=True, outgoing=True, command=alias_command("z"),
          description='同时转换为 Clash 与 Surge 订阅链接',
          parameters='<url / node>')
async def convert_all(_, msg: Message):
    try:
        # 获取原始消息内容（可能是回复消息或当前消息）
        message_raw = (
            msg.reply_to_message and (msg.reply_to_message.caption or msg.reply_to_message.text)
        ) or (msg.caption or msg.text)

        await msg.edit('转换中...')

        # 正则提取所有 URL
        url_list = re.findall(
            r"[-A-Za-z0-9+&@#/%?=~_|!:,.;]+://[-A-Za-z0-9+&@#/%?=~_|!:,.;]+[-A-Za-z0-9+&@#/%=~_|]",
            message_raw
        )
        url_list = list(set(url_list))  # 去重
        if not url_list:
            raise ValueError("未找到有效链接")

        # 编码并组合链接（以 | 连接）
        encoded_urls = "|".join([parse.quote_plus(url) for url in url_list])

        # 构造 Clash 和 Surge 链接
        clash_link = (
            f"[[Clash]]({backend}sub?target=clash&url={encoded_urls}"
            f"&insert=false&config={parse.quote_plus(config)}&emoji=true&list=false"
            f"&tfo=false&expand=true&scv=true&fdn=false&new_name=true&udp=true)"
        )
        surge_link = (
            f"[[Surge]]({backend}sub?target=surge&ver=4&url={encoded_urls}"
            f"&insert=false&config={parse.quote_plus(config)}&emoji=true&list=false"
            f"&tfo=false&expand=true&scv=true&fdn=false&new_name=true&udp=true)"
        )

        # 输出：换行展示
        await msg.edit(f"{clash_link}  {surge_link}")

    except Exception as e:
        await msg.edit(f"参数错误：{str(e)}")
