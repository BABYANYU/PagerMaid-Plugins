import os
import shutil
from pyrogram.types import Message
from pagermaid.listener import listener
from pagermaid.utils import alias_command


@listener(
    is_plugin=True,
    outgoing=True,
    command=alias_command("t"),
    description="将YAML文件转换为TXT格式",
    parameters="回复一个YAML文件并输入指令",
)
async def yaml_to_txt(client, msg: Message):
    try:
        # 原始 YAML 消息
        source = msg.reply_to_message
        if not source:
            await msg.edit("请回复一个YAML文件并输入指令。")
            return

        if not source.document:
            await msg.edit("回复的消息中没有文件。")
            return

        file_name = source.document.file_name or ""
        file_size = source.document.file_size or 0

        if file_size > 5 * 1024 * 1024:
            await msg.edit("文件过大，请提供小于 5MB 的文件。")
            return

        if not file_name.lower().endswith((".yaml", ".yml")):
            await msg.edit("不支持的文件格式，请提供YAML文件。")
            return

        # 下载原始 YAML
        file_path = await source.download()

        # 保留原文件名，只将 .yaml/.yml 改成 .txt
        output_name = os.path.splitext(file_name)[0] + ".txt"
        output_file = os.path.join(os.path.dirname(file_path), output_name)

        # 按字节复制，不解析 YAML，不增加任何文字，保证内容完全一致
        shutil.copyfile(file_path, output_file)

        # 明确指定 reply_to_message_id 为“原始 YAML”的消息 ID。
        # 不使用 msg.reply_document，避免回复到随后被删除的指令消息。
        await client.send_document(
            chat_id=msg.chat.id,
            document=output_file,
            reply_to_message_id=source.id,
        )

        # 上传成功后只删除 .t 指令消息
        await msg.delete()

    except Exception as e:
        await msg.edit(f"转换文件时出错：{str(e)}")
