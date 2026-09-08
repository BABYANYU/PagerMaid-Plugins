import platform
import tarfile
import json
import re
import math
import datetime
from PIL import Image
from asyncio import create_subprocess_shell
from asyncio.subprocess import PIPE
from json import loads
from os import makedirs
from os.path import exists
from pagermaid.listener import listener
from pagermaid.enums import Message, AsyncClient
from pagermaid.utils import lang, safe_remove

speedtest_path = "/var/lib/pagermaid/plugins/speedtest"
speedtest_json = "/var/lib/pagermaid/plugins/speedtest.json"

def get_default_server():
    if exists(speedtest_json):
        with open(speedtest_json, "r") as f:
            return json.load(f).get("default_server_id", None)
    return None

def save_default_server(server_id=None):
    with open(speedtest_json, "w") as f:
        json.dump({"default_server_id": server_id}, f)

def remove_default_server():
    if exists(speedtest_json):
        safe_remove(speedtest_json)

async def get_latest_version(request: AsyncClient):
    try:
        response = await request.get("https://install.speedtest.net/app/cli/")
        machine = "x86_64" if platform.machine() == "AMD64" else platform.machine()
        pattern = rf'ookla-speedtest-([0-9]+\.[0-9]+\.[0-9]+)-linux-{machine}\.tgz'
        matches = re.findall(pattern, response.text, re.IGNORECASE)
        return max(matches, key=lambda v: tuple(map(int, v.split('.')))) if matches else "1.2.0"
    except Exception:
        return "1.2.0"

async def download_cli(request: AsyncClient):
    latest_version = await get_latest_version(request)
    machine = "x86_64" if platform.machine() == "AMD64" else platform.machine()
    path = "/var/lib/pagermaid/plugins/"
    
    if not exists(path):
        makedirs(path)
    
    for version in [latest_version, "1.2.0"]:
        try:
            filename = f"ookla-speedtest-{version}-linux-{machine}.tgz"
            data = await request.get(f"https://install.speedtest.net/app/cli/{filename}")
            with open(path + filename, mode="wb") as f:
                f.write(data.content)
            
            tar = tarfile.open(path + filename, "r:gz")
            tar.extractall(path)
            tar.close()
            
            for file in [filename, "speedtest.5", "speedtest.md"]:
                safe_remove(f"{path}{file}")
            break
        except Exception:
            continue

    proc = await create_subprocess_shell(f"chmod +x {speedtest_path}", stdout=PIPE, stderr=PIPE)
    await proc.communicate()
    return path if exists(f"{path}speedtest") else None

def decode_output(output):
    try:
        return output.decode().strip()
    except UnicodeDecodeError:
        return output.decode("gbk").strip()

async def start_speedtest(command):
    proc = await create_subprocess_shell(command, stdout=PIPE, stderr=PIPE)
    stdout, stderr = await proc.communicate()
    return decode_output(stdout), decode_output(stderr), proc.returncode

async def unit_convert(byte, is_bytes=False):
    power = 1000
    zero = 0
    units = {0: '', 1: 'KB', 2: 'MB', 3: 'GB', 4: 'TB'} if is_bytes else {0: '', 1: 'Kbps', 2: 'Mbps', 3: 'Gbps', 4: 'Tbps'}
    if not is_bytes:
        byte *= 8
    while byte > power:
        byte /= power
        zero += 1
    return f"{round(byte, 2)}{units[zero]}"

async def get_ip_api(request: AsyncClient, ip: str):
    try:
        response = await request.get(f"http://ip-api.com/json/{ip}?fields=as,country,countryCode")
        data = response.json()
        as_info = data.get('as', '').split()[0]
        cc_code = data.get('countryCode', '')
        cc_flag = ''.join([chr(127397 + ord(c)) for c in cc_code.upper()]) if cc_code else ''
        cc_link = f"https://www.submarinecablemap.com/country/{data.get('country', '').lower().replace(' ', '-')}"
        return as_info, cc_code, cc_flag, cc_link
    except Exception:
        return '', '', '', ''

def crop_rounded_corners(image_path):
    """裁切掉圆角区域"""
    try:
        img = Image.open(image_path)
        width, height = img.size
        crop_pixels = 5
        cropped = img.crop((crop_pixels, crop_pixels, width - crop_pixels, height - crop_pixels))
        cropped.save(image_path, 'PNG')
        return True
    except Exception:
        return False

async def save_speedtest_image(request, url):
    """下载并裁切测速图片"""
    try:
        data = await request.get(url + '.png')
        with open("speedtest.png", mode="wb") as f:
            f.write(data.content)
        
        if exists("speedtest.png"):
            crop_rounded_corners("speedtest.png")
            return "speedtest.png"
    except Exception:
        pass
    return None

async def get_user_location(request: AsyncClient):
    services = [
        "http://ip-api.com/json/?fields=country,countryCode,regionName,city,lat,lon",
        "https://ipapi.co/json/",
        "https://ipinfo.io/json"
    ]
    
    for service in services:
        try:
            response = await request.get(service)
            if response.status_code == 200:
                data = response.json()
                
                if 'country' in data and 'lat' in data:
                    return {
                        'country': data.get('country', ''),
                        'countryCode': data.get('countryCode', ''),
                        'city': data.get('city', ''),
                        'lat': data.get('lat'),
                        'lon': data.get('lon')
                    }
                elif 'country_name' in data:
                    return {
                        'country': data.get('country_name', ''),
                        'countryCode': data.get('country', ''),
                        'city': data.get('city', ''),
                        'lat': data.get('latitude'),
                        'lon': data.get('longitude')
                    }
                elif 'loc' in data:
                    lat, lon = data.get('loc', '0,0').split(',')
                    return {
                        'country': data.get('country', ''),
                        'countryCode': data.get('country', ''),
                        'city': data.get('city', ''),
                        'lat': float(lat),
                        'lon': float(lon)
                    }
        except Exception:
            continue
    return None

def calculate_distance(lat1, lon1, lat2, lon2):
    try:
        lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])
        dlat = lat2 - lat1
        dlon = lon2 - lon1
        a = math.sin(dlat/2)**2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon/2)**2
        c = 2 * math.asin(math.sqrt(a))
        return c * 6371
    except:
        return float('inf')

async def get_servers(request: AsyncClient):
    if not exists(speedtest_path):
        await download_cli(request)
    
    servers_found = []
    user_location = await get_user_location(request)
    
    # API获取
    try:
        search_keywords = [""]
        if user_location:
            if user_location.get('country'):
                search_keywords.insert(0, user_location['country'])
            if user_location.get('city'):
                search_keywords.insert(0, user_location['city'])
        
        for keyword in search_keywords[:2]:
            api_url = f"https://www.speedtest.net/api/js/servers?limit=30" + (f"&search={keyword}" if keyword else "")
            response = await request.get(api_url)
            if response.status_code == 200:
                data = response.json()
                if isinstance(data, list):
                    for server in data:
                        if server.get('id') and server.get('name'):
                            distance = float('inf')
                            if user_location and server.get('lat') and server.get('lon'):
                                distance = calculate_distance(
                                    user_location['lat'], user_location['lon'],
                                    float(server['lat']), float(server['lon'])
                                )
                            
                            servers_found.append({
                                'id': server['id'],
                                'name': server.get('sponsor', server.get('name', '')),
                                'distance': distance
                            })
    except Exception:
        pass
    
    # CLI补充
    if len(servers_found) < 10:
        try:
            outs, errs, code = await start_speedtest(f"sudo {speedtest_path} -f json -L")
            if code == 0:
                result = loads(outs)
                if result and 'servers' in result:
                    for server in result['servers']:
                        if not any(s['id'] == server['id'] for s in servers_found):
                            servers_found.append({'id': server['id'], 'name': server['name']})
        except Exception:
            pass
    
    # 去重并排序
    unique_servers = []
    seen_ids = set()
    for server in servers_found:
        if server['id'] not in seen_ids:
            seen_ids.add(server['id'])
            unique_servers.append(server)
    
    unique_servers.sort(key=lambda x: x.get('distance', float('inf')))
    return unique_servers[:20], user_location

async def run_speedtest(request: AsyncClient, message: Message):
    if not exists(speedtest_path):
        await download_cli(request)

    server_id = message.arguments if message.arguments.isdigit() else get_default_server()
    command = f"sudo {speedtest_path} --accept-license --accept-gdpr -f json" + (f" -s {server_id}" if server_id else "")
    outs, errs, code = await start_speedtest(command)

    if code == 0:
        result = loads(outs)
    elif errs and "NoServersException" in errs:
        return "无法连接到指定服务器", None
    else:
        return "网速测试失败", None
        
    as_info, cc_code, cc_flag, cc_link = await get_ip_api(request, result['interface']['externalIp'])
    current_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    # 处理网卡信息，去掉MTU
    interface_info = ""
    if 'interface' in result:
        interface = result['interface']
        conn_type = "IPv4" if not interface.get('isVpn', False) else "VPN"
        interface_name = interface.get('name', '未知')
        interface_info = f"{conn_type} - {interface_name}"
    
    des = (
        f"**🚀 网速测试结果** [@{cc_code}{cc_flag}]({cc_link})\n\n"
        f"• **服务商名:** {result['isp']} [{as_info}](https://bgp.tools/{as_info})\n"
        f"• **测试节点:** {result['server']['name']} ({result['server']['id']})\n"
        f"• **网卡信息:** {interface_info}\n"
        f"• **连接延迟:** {result['ping']['latency']}ms (±{result['ping']['jitter']}ms)\n"
        f"• **下载速度:** {await unit_convert(result['download']['bandwidth'])}\n"
        f"• **上传速度:** {await unit_convert(result['upload']['bandwidth'])}\n"
        f"• **测试时间:** {current_time}"
    )

    photo = None
    share_url = result.get("result", {}).get("url")
    if share_url:
        photo = await save_speedtest_image(request, share_url)
    
    return des, photo

def get_help_text():
    return (
        "**🚀 Speedtest Ookla 测速工具**\n\n"
        "**基本命令:**\n"
        "• `st` - 开始网速测试\n"
        "• `st <服务器ID>` - 使用指定服务器测试\n"
        "• `st list` - 查看可用服务器列表\n\n"
        "**配置命令:**\n"
        "• `st set <服务器ID>` - 设置默认服务器\n"
        "• `st clear` - 清除默认服务器设置\n"
        "• `st config` - 查看当前配置\n"
        "• `st update` - 更新测速工具\n"
        "• `st help` - 显示此帮助信息\n\n"
        "**使用示例:**\n"
        "`st` - 自动选择最优服务器测速\n"
        "`st 1536` - 使用ID为1536的服务器测速\n"
        "`st set 1536` - 设置1536为默认服务器"
    )

@listener(command="st", description="Speedtest by Ookla 网速测试工具", parameters="(ID/list/set/clear/config/update/help)")
async def speedtest_ookla(client, message: Message, request: AsyncClient):
    msg = message
    args = message.arguments.strip()
    
    if args == "help":
        return await msg.edit(get_help_text())
    
    elif args == "list":
        servers_found, user_location = await get_servers(request)
        if not servers_found:
            return await msg.edit("未找到可用服务器")
        
        server_lines = [f"• `{s['id']}` - `{s['name']}`" for s in servers_found]
        server_text = "\n".join(server_lines)
        
        location_info = ""
        if user_location and user_location.get('city'):
            cc_code = user_location.get('countryCode', '')
            cc_flag = ''.join([chr(127397 + ord(c)) for c in cc_code.upper()]) if cc_code else ''
            location_info = f"📍 **当前位置:** {user_location['city']}, {cc_flag} {user_location.get('country', '')}\n\n"
        
        return await msg.edit(
            f"**🌐 可用测速服务器**\n\n"
            f"{location_info}"
            f"📊 **共找到 {len(servers_found)} 个服务器:**\n\n"
            f"{server_text}\n\n"
            f"💡 **使用方法:** `st <ID>` 选择特定服务器测速"
        )
    
    elif args.startswith("set"):
        try:
            server_id = args.split()[1]
            save_default_server(server_id)
            return await msg.edit(f"**⚙️ 配置更新**\n\n• 默认服务器已设置为 `{server_id}`")
        except IndexError:
            return await msg.edit(f"**⚙️ 配置错误**\n\n• **用法:** `st set <服务器ID>`")
    
    elif args == "clear":
        remove_default_server()
        return await msg.edit(f"**⚙️ 配置更新**\n\n• 默认服务器设置已清除")
    
    elif args == "config":
        server_id = get_default_server() or "自动选择"
        try:
            outs, errs, code = await start_speedtest(f"{speedtest_path} --version")
            if code == 0:
                version_lines = outs.split('\n')
                tool_version = version_lines[0].split('Linux/')[0].strip() if version_lines else "未知版本"
            else:
                tool_version = "未知版本"
        except:
            tool_version = "未知版本"
            
        return await msg.edit(
            f"**⚙️ 当前配置**\n\n"
            f"🎯 **默认服务器：**\n"
            f"`{server_id}`\n\n"
            f"🔧 **工具版本：**\n"
            f"`{tool_version}`\n\n"
            f"💻 **系统内核：**\n"
            f"`{platform.release()}`\n\n"
            f"📁 **安装路径：**\n"
            f"`{speedtest_path}`"
        )
    
    elif args == "update":
        try:
            await download_cli(request)
            return await msg.edit(f"**🔄 更新完成**\n\n• 测速工具已更新到最新版本")
        except Exception as e:
            return await msg.edit(f"**🔄 更新失败**\n\n• **错误:** {str(e)}")
    
    elif not args or args.isdigit():
        msg = await message.edit("正在进行网速测试，请稍候...")
        des, photo = await run_speedtest(request, message)
    else:
        return await msg.edit(f"**❌ 参数错误**\n\n💡 **使用方法:** `st help` 查看帮助信息")

    if not photo:
        return await msg.edit(des)

    try:
        await client.send_file(message.chat_id, photo, caption=des)
        await msg.safe_delete()
    except Exception:
        await msg.edit(des)
    finally:
        safe_remove(photo)