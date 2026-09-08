import os
import re
import time
import urllib
import base64
import sqlite3
import requests
from datetime import datetime

import contextlib

from pyrogram.types import Message

from pyrogram.errors import Flood
from pyrogram.errors.exceptions.bad_request_400 import ChatForwardsRestricted

from pagermaid.listener import listener
from pagermaid.enums import Client, Message
from pagermaid.dependence import client as http_client

conn = sqlite3.connect('My_sub.db', check_same_thread=False)
c = conn.cursor()
c.execute('''CREATE TABLE IF NOT EXISTS My_sub(URL text, comment text)''')

items_per_page = 20
callbacks = {}

admin_id = []

current_lib = "My_sub"

send_percent = 5

def lib_name_change(lib_name):
    if lib_name == "My_sub" :
        return "My_sub"
    elif lib_name == "Default":
        return "My_sub"
    elif lib_name == "默认":
        return "My_sub"
    elif lib_name == "默认库":
        return "My_sub"
    elif lib_name == "默认仓库":
        return "My_sub"
    else:
        return lib_name

def load_admin() :
    global admin_id
    admin_id = []
    try :
        r = open('submanager_admin.txt')
        while True :
            line = r.readline()
            if not line :
                break
            id = line.strip()
            admin_id.append(id)
    except :
        pass

def save_admin() :
    try :
        with open("submanager_admin.txt","w") as f :
            for id in admin_id :
                f.write(id + '\n')
    except :
        pass

def convert_time_to_str(ts):
    return str(ts).zfill(2)

def sec_to_data(y):
    h = int(y // 3600 % 24)
    d = int(y // 86400)
    h = convert_time_to_str(h)
    d = convert_time_to_str(d)
    return d + "天" + h + "小时"


def StrOfSize(size):
    def strofsize(integer, remainder, level):
        if integer >= 1024:
            remainder = integer % 1024
            integer //= 1024
            level += 1
            return strofsize(integer, remainder, level)
        elif integer < 0:
            integer = 0
            return strofsize(integer, remainder, level)
        else:
            return integer, remainder, level

    units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB', 'EB', 'ZB', 'YB']
    integer, remainder, level = strofsize(size, 0, 0)
    if level + 1 > len(units):
        level = -1
    return ('{}.{:>03d} {}'.format(integer, remainder, units[level]))


@listener(command="s", description="自走型订阅管理")
async def submanager(bot: Client, message: Message):
    sent_message = message
    global admin_id, current_lib, connect_bot
    try :
        load_admin()
        admin_id = list(set(admin_id))
        save_admin()
    except :
        pass
    try :
        if not message.from_user.is_self == True :
            if not str(message.from_user.id) in admin_id :
                return
    except :
        return
        
    try :
        command = message.text.split()[1]
        sent_message.edit("接收命令中...")
    except :
        await sent_message.edit("命令不对呢")
        await sent_message.delay_delete(2)
        return
    if command == "查询":
        try :
            sent_message = await message.edit("查询中...")
            try :
                search_str = message.text.split()[2]
            except :
                 await sent_message.edit("命令格式不对呢")
                 await sent_message.delay_delete(2)
                 return
            try :
                current_page = int(message.text.split()[3])
            except :
                current_page = 1

            if current_lib[0] == "@":
                connect_command = "/connect index " + search_str + " " + str(current_page)
                callbacks[sent_message.id] = {'search' : search_str, 'lib' : current_lib}
                await bot.send_message(current_lib, connect_command)
                async with bot.conversation(current_lib) as conv:
                    chat_response = await conv.get_response()
                    await conv.mark_as_read()
                output_text = base64.b64decode(chat_response.text).decode('utf-8')
                if output_text == "None":
                    await sent_message.edit("查询失败")
                    await sent_message.delay_delete(2)
                    return
                message_raw = "查询到 `" + output_text.split()[0] + "` 条订阅\n第 `" + output_text.split()[1] + "` 页 共 `" + output_text.split()[2] + "` 页\n\n"
                for i in range(3, len(output_text.split()), 2):
                    message_raw = message_raw + "编号 `" + output_text.split()[i] + "  " + output_text.split()[i + 1] + "`\n"
                await sent_message.edit(message_raw)
                return
        
            sql_command = "SELECT rowid,URL,comment FROM " + current_lib + " WHERE URL LIKE ? OR comment LIKE ?"
            c.execute(sql_command,('%' + search_str + '%', '%' + search_str + '%'))
            result = c.fetchall()
            callbacks[sent_message.id] = {'search' : search_str, 'lib' : current_lib}
            pages = [result[i:i + items_per_page] for i in range(0, len(result), items_per_page)]
            current_items = pages[current_page - 1]
            message_raw = "查询到 `" + str(len(result)) + "` 条订阅\n第 `" + str(current_page) + "` 页 共 `" + str(len(pages)) + "` 页\n\n"
            for i in range(0, len(current_items)):
                sub = current_items[i]
                message_raw = message_raw + "编号 `" + str(sub[0]) + "  " + str(sub[2]) + "`\n"
            await sent_message.edit(message_raw)
        except :
            await sent_message.edit("查询失败")
            await sent_message.delay_delete(2)
    elif command == "翻页":
        try :
            if message.reply_to_message :
                reply = message.reply_to_message
                try :
                    current_page = int(message.text.split()[2])
                except :
                     sent_message = await message.edit("命令格式不对呢")
                     await sent_message.delay_delete(2)
                     return
            else :
                sent_message = await message.edit('请回复订阅消息呢')
                await sent_message.delay_delete(2)
                return
            search_str = callbacks[reply.id]['search']
            if callbacks[reply.id]['lib'][0] == "@":
                connect_command = "/connect index " + search_str + " " + str(current_page)
                await bot.send_message(callbacks[reply.id]['lib'], connect_command)
                async with bot.conversation(callbacks[reply.id]['lib']) as conv:
                    chat_response = await conv.get_response()
                    await conv.mark_as_read()
                output_text = base64.b64decode(chat_response.text).decode('utf-8')
                if output_text == "None":
                    await sent_message.edit("翻页失败")
                    await sent_message.delay_delete(2)
                    await message.delay_delete(5)
                    return
                message_raw = "查询到 `" + output_text.split()[0] + "` 条订阅\n第 `" + output_text.split()[1] + "` 页 共 `" + output_text.split()[2] + "` 页\n\n"
                for i in range(3, len(output_text.split()), 2):
                    message_raw = message_raw + "编号 `" + output_text.split()[i] + "  " + output_text.split()[i + 1] + "`\n"
                await message.delay_delete(0)
                await reply.edit(message_raw)
                return
            sql_command = "SELECT rowid,URL,comment FROM " + callbacks[reply.id]['lib'] + " WHERE URL LIKE ? OR comment LIKE ?"
            c.execute(sql_command,('%' + search_str + '%', '%' + search_str + '%'))
            result = c.fetchall()
            pages = [result[i:i + items_per_page] for i in range(0, len(result), items_per_page)]
            current_items = pages[current_page - 1]
            message_raw = "查询到 `" + str(len(result)) + "` 条订阅\n第 `" + str(current_page) + "` 页 共 `" + str(len(pages)) + "` 页\n\n"
            for i in range(0, len(current_items)):
                sub = current_items[i]
                message_raw = message_raw + "编号 `" + str(sub[0]) + "  " + str(sub[2]) + "`\n"
            await message.delay_delete(0)
            await reply.edit(message_raw)
        except :
            await sent_message.edit("翻页失败")
            await sent_message.delay_delete(2)
            await message.delay_delete(5)
    elif command == "添加":
        try :
            try :
                url_comment = message.text.split()[2:]
                url = url_comment[0]
                comment = url_comment[1]
            except :
                 await sent_message.edit("命令格式不对呢")
                 await sent_message.delay_delete(2)
                 return
            sql_command = "SELECT * FROM " + current_lib + " WHERE URL LIKE ?"
            c.execute(sql_command, (url,))
            if c.fetchone():
                await sent_message.edit("订阅已存在")
            else:
                sql_command = "INSERT INTO " + current_lib + " VALUES(?,?)"
                c.execute(sql_command, (url, comment))
                conn.commit()
                await sent_message.edit("订阅 `" + comment + "` 添加成功")
                await sent_message.delay_delete(2)
        except :
            await sent_message.edit("添加失败")
            await sent_message.delay_delete(2)
    elif command == "更新":
        try :
            try :
                row_num = message.text.split()[2]
                url_comment = message.text.split()[3:]
                url = url_comment[0]
                comment = url_comment[1]
            except :
                 await sent_message.edit("命令格式不对呢")
                 await sent_message.delay_delete(2)
                 return

            sql_command = "UPDATE " + current_lib + " SET URL=?, comment=? WHERE rowid=?"
            c.execute(sql_command, (url, comment, row_num))
            conn.commit()
            await sent_message.edit("编号 `" + str(row_num) + "` 更新成功")
            await sent_message.delay_delete(2)
        except :
            await sent_message.edit("更新失败")
            await sent_message.delay_delete(2)
    elif command == "删除":
        try :
            try :
                row_num = message.text.split()[2]
            except :
                 await sent_message.edit("命令格式不对呢")
                 await sent_message.delay_delete(2)
                 return

            sql_command = "DELETE FROM " + current_lib + " WHERE rowid=?"
            c.execute(sql_command, (row_num,))
            conn.commit()
            await sent_message.edit("编号 `" + str(row_num) + "` 删除成功")
            await sent_message.delay_delete(2)
        except :
            await sent_message.edit("删除失败")
            await sent_message.delay_delete(2)
    elif command == "整理":
        try :
            c.execute("VACUUM")
            conn.commit()
            await sent_message.edit("整理成功")
            await sent_message.delay_delete(2)
        except :
            await sent_message.edit("整理失败")
            await sent_message.delay_delete(2)
    elif command == "查看":
        try :
            try :
                row_num = int(message.text.split()[2])
            except :
                 await sent_message.edit("命令格式不对呢")
                 await sent_message.delay_delete(2)
                 return

            if current_lib[0] == "@":
                connect_command = "/connect get " + str(row_num)
                await bot.send_message(current_lib, connect_command)
                async with bot.conversation(current_lib) as conv:
                    chat_response = await conv.get_response()
                    await conv.mark_as_read()
                output_text = base64.b64decode(chat_response.text).decode('utf-8')
                if output_text == "None":
                    await sent_message.edit("获取失败")
                    await sent_message.delay_delete(2)
                    return
                result = output_text.split()
            else:            
                sql_command = "SELECT rowid,URL,comment FROM " + current_lib + " WHERE rowid=?"
                c.execute(sql_command, (row_num,))
                result = c.fetchone()
            
            headers = {'User-Agent': 'ClashforWindows/0.18.1'}
            output_text = ''
            final_output = '编号 `' + str(result[0]) + '`\n订阅域名 `' + urllib.parse.urlparse(result[1]).netloc + '`\n说明 `' + str(result[2]) + '`\n\n'
            url = result[1]
            await sent_message.edit("查询中...")
            try:
                res = await http_client.get(url, headers=headers, timeout=5)
                while res.status_code == 301 or res.status_code == 302:
                    url1 = res.headers['location']
                    res = await http_client.get(url1, headers=headers, timeout=5)
            except:
                final_output = final_output + '连接错误' + '\n\n'
            if res.status_code == 200:
                try:
                    info = res.headers['subscription-userinfo']
                    info_num = re.findall('\d+', info)
                    time_now = int(time.time())
                    output_text_head = '已用上行：`' + StrOfSize(int(info_num[0])) + '`\n已用下行：`' + StrOfSize(int(info_num[1])) + '`\n剩余：`' + StrOfSize(int(info_num[2]) - int(info_num[1]) - int(info_num[0])) + '`\n总共：`' + StrOfSize(int(info_num[2]))
                    if len(info_num) >= 4:
                        timeArray = time.localtime(int(info_num[3]) + 28800)
                        dateTime = time.strftime("%Y-%m-%d", timeArray)
                        if time_now <= int(info_num[3]):
                            lasttime = int(info_num[3]) - time_now
                            output_text = output_text_head + '`\n过期时间：`' + dateTime + '`\n剩余时间：`' + sec_to_data(lasttime) + '`'
                        elif time_now > int(info_num[3]):
                            output_text = output_text_head + '`\n此订阅已于`' + dateTime + '`过期'
                    else:
                        output_text = output_text_head + '`\n到期时间：`没有说明`'
                except:
                    output_text = '无流量信息'
            else:
                output_text = '无法访问'
            final_output = final_output + output_text + '\n\n'
            await sent_message.edit(final_output)
        except :
            await sent_message.edit("获取失败")
            await sent_message.delay_delete(2)
    elif command == "获取":
        try :
            try :
                row_num = int(message.text.split()[2])
            except :
                 await sent_message.edit("命令格式不对呢")
                 await sent_message.delay_delete(2)
                 return

            if current_lib[0] == "@":
                connect_command = "/connect get " + str(row_num)
                await bot.send_message(current_lib, connect_command)
                async with bot.conversation(current_lib) as conv:
                    chat_response = await conv.get_response()
                    await conv.mark_as_read()
                output_text = base64.b64decode(chat_response.text).decode('utf-8')
                if output_text == "None":
                    await sent_message.edit("获取失败")
                    await sent_message.delay_delete(2)
                    return
                result = output_text.split()
            else:            
                sql_command = "SELECT rowid,URL,comment FROM " + current_lib + " WHERE rowid=?"
                c.execute(sql_command, (row_num,))
                result = c.fetchone()

            
            headers = {'User-Agent': 'ClashforWindows/0.18.1'}
            output_text = ''
            final_output = '编号 `' + str(result[0]) + '`\n订阅链接 `' + str(result[1]) + '`\n说明 `' + str(result[2]) + '`\n\n'
            url = result[1]
            await sent_message.edit("查询中...")
            try:
                res = await http_client.get(url, headers=headers, timeout=5)
                while res.status_code == 301 or res.status_code == 302:
                    url1 = res.headers['location']
                    res = await http_client.get(url1, headers=headers, timeout=5)
            except:
                final_output = final_output + '连接错误' + '\n\n'
            if res.status_code == 200:
                try:
                    info = res.headers['subscription-userinfo']
                    info_num = re.findall('\d+', info)
                    time_now = int(time.time())
                    output_text_head = '已用上行：`' + StrOfSize(int(info_num[0])) + '`\n已用下行：`' + StrOfSize(int(info_num[1])) + '`\n剩余：`' + StrOfSize(int(info_num[2]) - int(info_num[1]) - int(info_num[0])) + '`\n总共：`' + StrOfSize(int(info_num[2]))
                    if len(info_num) >= 4:
                        timeArray = time.localtime(int(info_num[3]) + 28800)
                        dateTime = time.strftime("%Y-%m-%d", timeArray)
                        if time_now <= int(info_num[3]):
                            lasttime = int(info_num[3]) - time_now
                            output_text = output_text_head + '`\n过期时间：`' + dateTime + '`\n剩余时间：`' + sec_to_data(lasttime) + '`'
                        elif time_now > int(info_num[3]):
                            output_text = output_text_head + '`\n此订阅已于`' + dateTime + '`过期'
                    else:
                        output_text = output_text_head + '`\n到期时间：`没有说明`'
                except:
                    output_text = '无流量信息'
            else:
                output_text = '无法访问'
            final_output = final_output + output_text + '\n\n'
            await sent_message.edit(final_output)
        except :
            await sent_message.edit("获取失败")
            await sent_message.delay_delete(2)
    elif command == "交换":
        try :
            try :
                row_num1 = int(message.text.split()[2])
                row_num2 = int(message.text.split()[3])
            except :
                 await sent_message.edit("命令格式不对呢")
                 await sent_message.delay_delete(2)
                 return
            c.execute("SELECT rowid,URL,comment FROM " + current_lib + " WHERE rowid=?", (row_num1,))
            result1 = c.fetchone()
            c.execute("SELECT rowid,URL,comment FROM " + current_lib + " WHERE rowid=?", (row_num2,))
            result2 = c.fetchone()
            c.execute("UPDATE " + current_lib + " SET URL=?, comment=? WHERE rowid=?", (result2[1], result2[2], row_num1))
            c.execute("UPDATE " + current_lib + " SET URL=?, comment=? WHERE rowid=?", (result1[1], result1[2], row_num2))
            await sent_message.edit(f"已交换编号为 `{row_num1}` 和 `{row_num2}` 的订阅")
            await sent_message.delay_delete(2)
        except :
            await sent_message.edit("交换失败")
            await sent_message.delay_delete(2)
    elif command == "命名":
        try :
            try :
                row_num = int(message.text.split()[2])
                comment = message.text.split()[3]
            except :
                 await sent_message.edit("命令格式不对呢")
                 await sent_message.delay_delete(2)
                 return
            c.execute("UPDATE " + current_lib + " SET comment=? WHERE rowid=?", (comment, row_num))
            conn.commit()
            await sent_message.edit(f"已将编号 `{row_num}` 的订阅命名为 `{comment}`")
            await sent_message.delay_delete(2)
        except :
            await sent_message.edit("命名失败")
            await sent_message.delay_delete(2)
    elif command == "授权" :
        if not message.from_user.is_self == True :
            return
        sent_message = await message.edit("授权中...")
        try :
            id_text = message.text.split()
            if len(id_text) < 3:
                if not str(message.reply_to_message.from_user.id) in admin_id :
                    admin_id.append(str(message.reply_to_message.from_user.id))
            else:
                for i in id_text[2:]:
                    if not i in admin_id :
                        admin_id.append(i)
            admin_id = list(set(admin_id))
            save_admin()
            await sent_message.edit("授权成功")
            await sent_message.delay_delete(2)
        except :
            await sent_message.edit("授权失败")
            await sent_message.delay_delete(2)
    elif command == "销权" :
        if not message.from_user.is_self == True :
            return
        sent_message = await message.edit("销权中...")
        try :
            id_text = message.text.split()
            if len(id_text) < 3:
                if str(message.reply_to_message.from_user.id) in admin_id :
                    admin_id.remove(str(message.reply_to_message.from_user.id))
            else:
                for i in id_text[2:]:
                    if i in admin_id :
                        admin_id.remove(i)
            admin_id = list(set(admin_id))
            save_admin()
            await sent_message.edit("销权成功")
            await sent_message.delay_delete(2)
        except :
            await sent_message.edit("销权失败")
            await sent_message.delay_delete(2)
    elif command == "列表" :
        if not message.from_user.is_self == True :
            return
        sent_message = await message.edit("获取管理列表中...")
        try :
            id_text = ""
            for id in admin_id :
                id_text = id_text + str(id) + "\n"
            await sent_message.edit(id_text)
        except :
            await sent_message.edit("获取管理列表失败")
            await sent_message.delay_delete(2)
    elif command == "切换" :
        if not message.from_user.is_self == True :
            return
        sent_message = await message.edit("切换仓库中...")
        try :
            try :
                current_lib = message.text.split()[2]
            except :
                await sent_message.edit("命令格式不对呢")
                await sent_message.delay_delete(2)
                return
            current_lib = lib_name_change(current_lib)
            if current_lib == "My_sub" :
                await sent_message.edit("已将仓库切换为 `默认仓库`")
                try :
                    c.execute('''CREATE TABLE IF NOT EXISTS ''' + current_lib + '''(URL text, comment text)''')
                except:
                    pass
                await sent_message.delay_delete(2)
            else:
                try :
                    c.execute('''CREATE TABLE IF NOT EXISTS ''' + current_lib + '''(URL text, comment text)''')
                except:
                    pass
                temp_text = "已将仓库切换为 `" + current_lib + "`"
                await sent_message.edit(temp_text)
                await sent_message.delay_delete(2)
            
        except :
            await sent_message.edit("切换仓库失败")
            await sent_message.delay_delete(2)
    elif command == "列出库" :
        if not message.from_user.is_self == True :
            return
        sent_message = await message.edit("查询所有仓库中...")
        c.execute("select name from sqlite_master where type='table'")
        result = c.fetchall()
        message_raw = "查询到 `" + str(len(result)) + "` 个仓库"
        for lib in result:
            for i in lib:
                if i == 'My_sub':
                    i = "默认仓库"
                message_raw = message_raw + '\n`' + i + '`'
        await sent_message.edit(message_raw)
    elif command == "删除库":
        try :
            try :
                lib = message.text.split()[2]
            except :
                await sent_message.edit("命令格式不对呢")
                await sent_message.delay_delete(2)
                return
            lib = lib_name_change(lib)
            sent_message = await message.edit("删除仓库中...")
            sql_command = "DROP TABLE " + lib
            c.execute(sql_command)
            conn.commit()
            current_lib = "My_sub"
            if lib == "My_sub":
                lib = "默认仓库"
            await sent_message.edit("仓库 `" + lib + "` 删除成功")
            await sent_message.delay_delete(2)
        except :
            await sent_message.edit("删除失败")
            await sent_message.delay_delete(2)
    elif command == "命名库":
        try :
            try :
                old_name = message.text.split()[2]
                new_name = message.text.split()[3]
            except :
                await sent_message.edit("命令格式不对呢")
                await sent_message.delay_delete(2)
                return
            old_name = lib_name_change(old_name)
            new_name = lib_name_change(new_name)
            sent_message = await message.edit("命名仓库中...")
            sql_command = "ALTER TABLE " + old_name + " RENAME TO " + new_name
            c.execute(sql_command)
            conn.commit()
            if current_lib == old_name:
                current_lib = new_name
            if old_name == "My_sub":
                old_name = "默认仓库"
            if new_name == "My_sub":
                new_name = "默认仓库"
            await sent_message.edit("仓库 `" + old_name + "` 已命名为 `" + new_name + "`")
            await sent_message.delay_delete(2)
        except :
            await sent_message.edit("命名失败")
            await sent_message.delay_delete(2)
    elif command == "测活":
        sent_message = await message.edit("准备测活中...")
        sql_command = "SELECT rowid,URL,comment FROM " + current_lib
        c.execute(sql_command)
        result = c.fetchall()
        total = len(result)
        sending_time = 0
        global send_percent
        expire = []
        i = 0
        for item in result:
            i = i + 1
            cal = i / total * 100
            if cal > sending_time:
                sending_time += send_percent
                equal_signs = int(cal / 5)
                space_count = 20 - equal_signs
                await sent_message.edit("正在获取订阅中\n\n [`" + "=" * equal_signs + " " * space_count + "`]\n\n目前剩余任务数量为: `" + str(total - i + 1) + "`")
            url = item[1]
            headers = {'User-Agent': 'ClashforWindows/0.18.1'}
            try:
                res = requests.get(url, headers=headers, timeout=5)
            except:
                pass
            c.execute("UPDATE My_sub SET URL=?, comment=? WHERE rowid=?", (item[1], item[2].replace('-失效', '').replace('-到期', ''), item[0]))
            conn.commit()
            try:
                u = re.findall('proxies:', res.text)[0]
                if u == "proxies:":
                    pass
            except:
                try:
                    text = res.text[:64]
                    text = base64.b64decode(text)
                    text = str(text)
                    if filter_base64(text):
                        pass
                    else:
                        try:
                            info = res.headers['subscription-userinfo']
                            info_num = re.findall(r'\d+', info)
                            time_now = int(time.time())
                            if int(info_num[2])-int(info_num[1])-int(info_num[0])<=1:
                                expire.append(item[0])
                                c.execute("UPDATE My_sub SET URL=?, comment=? WHERE rowid=?", (item[1], item[2], item[0]))
                                conn.commit()
                        except:
                            expire.append(item[0])
                            c.execute("UPDATE My_sub SET URL=?, comment=? WHERE rowid=?", (item[1], item[2], item[0]))
                            conn.commit()
                except:
                    try:
                        info = res.headers['subscription-userinfo']
                        info_num = re.findall(r'\d+', info)
                        time_now = int(time.time())
                        if int(info_num[2])-int(info_num[1])-int(info_num[0])<=1:
                            expire.append(item[0])
                            c.execute("UPDATE My_sub SET URL=?, comment=? WHERE rowid=?", (item[1], item[2], item[0]))
                            conn.commit()
                    except:
                        expire.append(item[0])
                        c.execute("UPDATE My_sub SET URL=?, comment=? WHERE rowid=?", (item[1], item[2], item[0]))
                        conn.commit()
        expire = list(set(expire))
        expire.sort()
        if current_lib == "My_sub":
            libname = "默认仓库"
        else:
            libname = current_lib
        message_raw = "当前订阅库 `" + libname + "` 中\n\n"
        if len(expire) == 0:
            message_raw = message_raw + "无失效订阅"
        else:
            message_raw = message_raw + "已失效订阅共 `" + str(len(expire)) + "` 条\n\n编号如下\n`"
            for id in expire:
                message_raw = message_raw + str(id) + " "
        message_raw = message_raw + "`"
        await sent_message.edit(message_raw)
    elif command == "上传" :
        try:
            await bot.send_document("me", "My_sub.db")
            await sent_message.edit("数据库已上传至 `Telegram收藏夹`")
            await sent_message.delay_delete(2)
        except:
            await sent_message.edit("上传数据库失败")
            await sent_message.delay_delete(2)
    elif command == "下载":
        try:
            target = message.reply_to_message
            if target.document is None:
                await sent_message.edit("下载数据库失败")
                await sent_message.delay_delete(2)
            try:
                os.remove('My_sub.db')
            except:
                pass
            file_path = await target.download(file_name='./My_sub.db')
            if file_path:
                await sent_message.edit("下载数据库成功")
                await sent_message.delay_delete(2)
        except:
            await sent_message.edit("下载数据库失败")
            await sent_message.delay_delete(2)
        return
    elif command == "help" :
        await sent_message.edit("查询订阅 `s 查询 关键词`\n添加订阅 `s 添加 订阅 名称`\n更新订阅 `s 更新 编号 订阅 名称`\n删除订阅 `s 删除 编号`\n获取订阅 `s 获取 编号`\n查看订阅 `s 查看 编号`\n整理订阅 `s 整理`\n测活订阅 `s 测活`\n列表翻页 `s 翻页 页数`\n订阅交换 `s 交换 编号 编号`\n订阅命名 `s 命名 编号 名称`\n切换仓库 `s 切换 仓库名`\n删除仓库 `s 删除库 仓库名`\n列出仓库 `s 列出`\n命名仓库 `s 命名库 旧库 新库`\n上传数据 `s 上传`\n下载数据 `s 下载`\n授权用户 `s 授权`\n取消授权 `s 销权`\n授权列表 `s 列表`")
    else :
        await sent_message.edit("没有这个命令呢")
        await sent_message.delay_delete(2)
