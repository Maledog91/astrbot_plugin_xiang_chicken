import base64
import json
import time
import aiohttp
import asyncio
import re
import os
import random
import io
import shutil
from PIL import Image
from pathlib import Path
from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register

CACHE_DIR = Path(__file__).parent / "cache"

@register("小祥找鸡", "蓝色健达奇趣蛋", "引用图片进行识图并提取社交账号，支持X与Kemono反查及看图功能。", "1.0")
class FindChicken(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        cfg = config or {}

        def to_list(val):
            if val is None: return []
            if isinstance(val, list): return [str(x).strip() for x in val if str(x).strip()]
            if isinstance(val, str): return [x.strip() for x in re.split(r'[,，\s|]+', val) if x.strip()]
            return []

        self.commands = to_list(cfg.get("command", ["小祥找鸡"])) or ["小祥找鸡"]
        self.look_cmds = to_list(cfg.get("look_cmd", ["小祥看图"])) or ["小祥看图"]
        self.api_key = str(cfg.get("serpapi_key", "")).strip()

        self.hl = "zh-cn"
        self.country = "at"
        self.enable_look_pic = bool(cfg.get("enable_look_pic", False))
        self.look_pic_timeout = int(cfg.get("look_pic_timeout", 300))
        self.search_cooldown_time = int(cfg.get("search_cooldown", 10))
        self.auto_recall_time = int(cfg.get("auto_recall_time", 60))

        self.enable_whitelist = bool(cfg.get("enable_whitelist", True))
        self.group_whitelist = set(to_list(cfg.get("group_whitelist", "")))
        self.look_pic_blacklist = set(to_list(cfg.get("look_pic_blacklist", "")))
        self.disabled_groups = set(to_list(cfg.get("disabled_groups", "")))

        self.look_pic_auth = {}
        self.search_cooldown = {}
        
        p_cfg = cfg.get("prompt_settings", {})
        self.prompt_borrow = self._get_prompt_str(p_cfg, "prompt_borrow", "用户试图白嫖色情图片。")
        self.prompt_used = self._get_prompt_str(p_cfg, "prompt_used", "用户看过了还要看。")
        self.prompt_expired = self._get_prompt_str(p_cfg, "prompt_expired", "用户超时未看。")
        self.prompt_cooldown = self._get_prompt_str(p_cfg, "prompt_cooldown", "用户急躁连发。")
        self.prompt_group_disabled = self._get_prompt_str(p_cfg, "prompt_group_disabled", "在禁用群找色情图片。")
        self.prompt_look_disabled = self._get_prompt_str(p_cfg, "prompt_look_disabled", "未开启看图。")
        self.prompt_no_reply = self._get_prompt_str(p_cfg, "prompt_no_reply", "连引用都不会。")

        self.no_auth_msgs = ["贵安。连自己动手查的尊严都舍弃了吗？只会像寄生虫一样蹭别人的成果，真是令人作呕。"]
        self.already_used_msgs = ["贪得无厌也该有个限度吧？像条饿狗一样毫无底线，快收起你那副下作的嘴脸。"]
        self.expired_msgs = ["连看个图都磨磨唧唧，连把握时间的本事都没有，活该错过。清醒一点，重新去查吧。"]
        self.disabled_msgs = ["贵安。规矩就是规矩，这里不是让你发泄肮脏私欲的垃圾桶。收起那些不堪入目的念头。"]
        self.look_pic_msgs = ["贵安。满脑子都是下贱废料，不知廉耻也该有个底线吧？谁允许你把这种令人反胃的癖好带到这里来的？"]
        self.no_reply_msgs = ["贵安。连最基本的引用规矩都不懂，你还能做什么？别拿些没头没脑的指令来烦我。"]
        
        self.msg_no_api = "❌ 未配置 SerpApi API Key，无法进行识图。"
        self.msg_no_img = "❌ 未能在引用的消息中提取到账号信息。"
        self.msg_protected = "❌ 目标账号已开启隐私保护（锁推），无法获取图片。"
        self.msg_start = "🔍 正在查询，请稍候..."
        self.msg_start_look = "🔍 正在提取图片，请稍候..."
        self.msg_not_found = "❌ 未能通过该图片识别到有效的社交账号。"
        self.msg_dl_fail = "❌ 图片下载失败，请检查 AstrBot 的网络或代理配置。"
        self.msg_error = "❌ 查询过程中发生异常，请前往 AstrBot 后台查看日志。"
        self.msg_no_img_found = "❌ 该账号近期推文中没有可提取的图片。"

        self.ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        CACHE_DIR.mkdir(parents=True, exist_ok=True)

        for f in CACHE_DIR.glob("*"):
            try:
                if f.is_file(): f.unlink()
                else: shutil.rmtree(f)
            except: pass

    def _get_prompt_str(self, p_cfg, key, default_str):
        val = p_cfg.get(key, [])
        if isinstance(val, list):
            return " ".join([str(x) for x in val if str(x).strip()]) or default_str
        return str(val) if val else default_str

    async def get_smart_roast(self, event, default_msgs, prompt_text):
        try:
            provider = self.context.get_using_provider()
            if provider:
                final_prompt = f"{prompt_text} 请结合你当前的人设，发挥创造力，每次生成的词句必须完全不同！直接输出嘲讽的话，绝不要带解释、不要有引号，严格控制在40字以内。"
                resp = await provider.text_chat(prompt=final_prompt, session_id=event.session_id)
                if resp and resp.completion_text:
                    return resp.completion_text.strip()
        except Exception as e:
            logger.error(f"【DS模型】生成嘲讽失败: {e}")
        return random.choice(default_msgs)

    async def recall_msg_later(self, bot, message_id, delay):
        if delay <= 0: return
        await asyncio.sleep(delay)
        try:
            await bot.call_action("delete_msg", message_id=message_id)
            logger.info(f"【自动撤回】消息 {message_id} 已成功撤回。")
        except Exception as e:
            logger.warning(f"【自动撤回】撤回失败: {e}")

    def extract_accounts(self, matches):
        rt_authors = []
        direct_authors = []
        kemono_cands = []
        reserved = {"home", "explore", "search", "login", "share", "i", "intent", "hashtag", "settings", "status"}

        for match in matches or []:
            if not isinstance(match, dict): continue
            link = str(match.get("link", "") or "")
            title = str(match.get("title", "") or "").strip()
            if not link: continue

            if re.search(r"(?i)\bReplies by\b", title):
                continue

            km = re.search(r"https?://(kemono\.[a-z]+)/([a-z0-9]+)/user/(\d+)", link, re.I)
            if km:
                domain, service, creator_id = km.groups()
                kemono_cands.append(f"kemono_{domain}_{service}_{creator_id}")
                continue

            is_mirror = any(m in link for m in ["sotwe.com", "twstalker.com", "nitter.", "twicomi.com"]) or "Twitter Profile" in title
            if is_mirror:
                m_mirror = re.findall(r"@([A-Za-z0-9_]{1,15})", title)
                for uid in m_mirror:
                    if uid.lower() not in reserved and uid.lower() not in {x.lower() for x in direct_authors}:
                        direct_authors.append(uid)
                continue

            if re.search(r"(?i)(?:x\.com|twitter\.com)", link):
                rt_matches = re.findall(r"(?i)\bRT\s*@([A-Za-z0-9_]{1,15})\b", title)
                if rt_matches:
                    for rt in rt_matches:
                        if rt.lower() not in reserved and rt.lower() not in {x.lower() for x in rt_authors}:
                            rt_authors.append(rt)
                    continue

                if any(bad in link for bad in ["/media", "/likes", "/search", "with_replies"]):
                    continue

                profile_uids = re.findall(r"@([A-Za-z0-9_]{1,15})", title)
                if profile_uids:
                    for uid in profile_uids:
                        if uid.lower() not in reserved and uid.lower() not in {x.lower() for x in direct_authors}:
                            direct_authors.append(uid)
                    continue

                m = re.search(r"(?i)(?:https?://)?(?:(?:www|mobile)\.)?(?:x\.com|twitter\.com)/([A-Za-z0-9_]{1,15})(?:/status/|/|$)", link)
                if m:
                    uid = m.group(1)
                    if uid.lower() not in reserved and uid.lower() not in {x.lower() for x in direct_authors}:
                        direct_authors.append(uid)

        return direct_authors, rt_authors, kemono_cands

    async def download_image(self, session, url, retries=3):
        headers = {"User-Agent": self.ua, "Accept": "text/css, image/webp, image/apng, image/*, */*"}
        fallback_url = None
        if "pbs.twimg.com" in url:
            fallback_url = url + ("&name=small" if "?" in url else "?name=small")

        for i in range(retries):
            try:
                async with session.get(url, headers=headers, timeout=20, ssl=False) as resp:
                    if resp.status == 200: return await resp.read()
            except Exception:
                if i == 1 and fallback_url:
                    try:
                        async with session.get(fallback_url, headers=headers, timeout=20, ssl=False) as resp:
                            if resp.status == 200: return await resp.read()
                    except Exception: pass
                await asyncio.sleep(1)
        return None

    async def get_true_name_and_followers(self, session, uid):
        if not uid: return "未知", 0
        try:
            async with session.get(f"https://api.fxtwitter.com/{uid}", headers={"User-Agent": self.ua}, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    user = data.get("user") or {}
                    if isinstance(user, dict):
                        name = user.get("name", "").strip()
                        followers = user.get("followers", 0)
                        try: followers = int(followers)
                        except: followers = 0
                        return (name or "未知"), followers
        except Exception: pass
        return "未知", 0

    async def cleanup_later(self, folder_path, delay=60):
        await asyncio.sleep(delay)
        try:
            if os.path.exists(folder_path): shutil.rmtree(folder_path)
        except Exception: pass

    @filter.regex(r".+")
    async def on_message(self, event: AstrMessageEvent):
        msg = event.message_str.strip()
        group_id = str(getattr(event.message_obj, "group_id", "") or "")
        user_id = event.get_sender_id()
        is_group = bool(group_id and group_id != "0")

        if is_group and self.enable_whitelist and group_id not in self.group_whitelist: return

        # ================= 看图逻辑 =================
        if msg in self.look_cmds:
            if not self.enable_look_pic or (is_group and group_id in self.look_pic_blacklist):
                yield event.plain_result(await self.get_smart_roast(event, self.look_pic_msgs, self.prompt_look_disabled))
                return

            raw_msg = event.message_obj.raw_message.get("message", [])
            segs = json.loads(raw_msg) if isinstance(raw_msg, str) else raw_msg
            reply_id = next((x.get("data", {}).get("id") for x in segs if isinstance(x, dict) and x.get("type") == "reply"), None)

            if not reply_id:
                yield event.plain_result(await self.get_smart_roast(event, self.no_reply_msgs, self.prompt_no_reply))
                return

            try:
                reply_msg_data = await event.bot.call_action("get_msg", message_id=int(reply_id))
                reply_text = json.dumps(reply_msg_data, ensure_ascii=False)
                match = re.search(r"@([A-Za-z0-9_]{1,15})", reply_text)
                if not match:
                    yield event.plain_result(self.msg_no_img)
                    return

                uid = match.group(1)
                auth_key = (group_id, user_id)
                current_time = time.time()

                if auth_key in self.look_pic_auth:
                    record = self.look_pic_auth[auth_key]
                    if current_time - record["time"] > self.look_pic_timeout:
                        del self.look_pic_auth[auth_key]
                        yield event.plain_result(await self.get_smart_roast(event, self.expired_msgs, self.prompt_expired))
                        return
                    if record["uid"] != uid:
                        yield event.plain_result(random.choice(self.no_auth_msgs))
                        return
                    del self.look_pic_auth[auth_key]
                else:
                    is_others = False
                    for k, v in list(self.look_pic_auth.items()):
                        if v["uid"] == uid and k[0] == group_id:
                            if current_time - v["time"] > self.look_pic_timeout: del self.look_pic_auth[k]
                            else: is_others = True; break
                    if is_others:
                        yield event.plain_result(await self.get_smart_roast(event, self.no_auth_msgs, self.prompt_borrow))
                    else:
                        yield event.plain_result(await self.get_smart_roast(event, self.already_used_msgs, self.prompt_used))
                    return

                yield event.plain_result(self.msg_start_look)
                async with aiohttp.ClientSession(trust_env=True) as session:
                    img_urls = []
                    try:
                        profile_url = f"https://api.fxtwitter.com/{uid}"
                        async with session.get(profile_url, headers={"User-Agent": self.ua}, timeout=10) as resp:
                            if resp.status == 200:
                                p_data = await resp.json(content_type=None)
                                user_info = p_data.get("user", {})
                                if user_info.get("protected"):
                                    logger.info(f"【看图拦截】账号 @{uid} 是锁推账号！")
                                    yield event.plain_result(self.msg_protected)
                                    return
                                pinned = user_info.get("pinned_tweet")
                                if pinned and isinstance(pinned, dict):
                                    p_text = str(pinned.get("text", "")).lower()
                                    if not any(bad in p_text for bad in ["shadowban", "shadow", "测试", "通知", "封禁"]):
                                        for p in pinned.get("media", {}).get("photos", []):
                                            if p.get("url"): img_urls.append(p.get("url"))
                    except Exception: pass

                    media_url = f"https://api.fxtwitter.com/2/profile/{uid}/media"
                    async with session.get(media_url, headers={"User-Agent": self.ua}, timeout=15) as resp:
                        if resp.status == 200:
                            data = await resp.json(content_type=None)
                            for tweet in data.get("results", []):
                                if tweet.get("reposted_by"): continue
                                t_text = str(tweet.get("text", "")).lower()
                                if any(bad in t_text for bad in ["shadowban", "shadow", "测试", "通知", "封禁"]): continue
                                for photo in tweet.get("media", {}).get("photos", []):
                                    if len(img_urls) >= 10: break
                                    url = photo.get("url")
                                    if url and url not in img_urls: img_urls.append(url)
                                if len(img_urls) >= 10: break

                    if not img_urls:
                        yield event.plain_result(self.msg_no_img_found)
                        return

                    temp_folder = CACHE_DIR / f"pic_{uid}_{int(time.time())}"
                    temp_folder.mkdir(parents=True, exist_ok=True)

                    b64_images = []
                    for i, url in enumerate(img_urls):
                        img_data = await self.download_image(session, url)
                        if img_data:
                            try:
                                img = Image.open(io.BytesIO(img_data))
                                if img.mode != "RGB": img = img.convert("RGB")
                                img.thumbnail((1920, 1920))
                                out_io = io.BytesIO()
                                img.save(out_io, format="JPEG", quality=90)
                                compressed_data = out_io.getvalue()
                            except Exception:
                                compressed_data = img_data
                            b64_str = base64.b64encode(compressed_data).decode("utf-8")
                            b64_images.append(f"base64://{b64_str}")

                    if not b64_images:
                        yield event.plain_result(self.msg_dl_fail)
                        return

                    nodes = []
                    self_id_int = 10000
                    try: self_id_int = int(event.get_self_id())
                    except: pass
                    raw_name = getattr(event.bot, "name", "客服小祥")
                    bot_name = "客服小祥" if not raw_name or not isinstance(raw_name, str) else raw_name

                    for b64 in b64_images:
                        nodes.append({"type": "node", "data": {"user_id": self_id_int, "nickname": bot_name, "content": [{"type": "image", "data": {"file": b64}}]}})

                    warning_text = "⚠️ 提示：识图与图库提取可能存在误差，请自行核对图片中的人物是否为你要找的本人。"
                    nodes.append({"type": "node", "data": {"user_id": self_id_int, "nickname": bot_name, "content": [{"type": "text", "data": {"text": warning_text}}]}})

                    try:
                        ret = None
                        if is_group: ret = await event.bot.call_action("send_group_forward_msg", group_id=int(group_id), messages=nodes)
                        else: ret = await event.bot.call_action("send_private_forward_msg", user_id=int(event.message_obj.sender.user_id), messages=nodes)
                        
                        if self.auto_recall_time > 0 and isinstance(ret, dict) and ret.get("message_id"):
                            asyncio.create_task(self.recall_msg_later(event.bot, int(ret["message_id"]), self.auto_recall_time))
                            
                    except Exception as ex:
                        logger.error(f"合并转发报错: {ex}")
                        for b64 in b64_images: yield event.make_result().base64_image(b64)
                    
                    asyncio.create_task(self.cleanup_later(temp_folder, 60))
            except Exception as e:
                logger.error(f"【看图日志】: {e}")
                yield event.plain_result(self.msg_error)
            return

        # ================= 识图找鸡逻辑 =================
        if msg not in self.commands: return

        if is_group and group_id in self.disabled_groups:
            yield event.plain_result(await self.get_smart_roast(event, self.disabled_msgs, self.prompt_group_disabled))
            return

        now_time = time.time()
        if is_group:
            last_time = self.search_cooldown.get(group_id, 0)
            if now_time - last_time < self.search_cooldown_time:
                yield event.plain_result(await self.get_smart_roast(event, ["急什么？赶着投胎吗？稍后再来。"], self.prompt_cooldown))
                return
            self.search_cooldown[group_id] = now_time

        if not self.api_key:
            yield event.plain_result(self.msg_no_api)
            return

        raw_msg = event.message_obj.raw_message.get("message", [])
        segs = json.loads(raw_msg) if isinstance(raw_msg, str) else raw_msg
        reply_id = next((x.get("data", {}).get("id") for x in segs if isinstance(x, dict) and x.get("type") == "reply"), None)
        if not reply_id:
            yield event.plain_result(await self.get_smart_roast(event, self.no_reply_msgs, self.prompt_no_reply))
            return

        tmp_path = None
        try:
            msg_data = await event.bot.call_action("get_msg", message_id=int(reply_id))
            img_data = None
            def walk_img(obj):
                nonlocal img_data
                if img_data: return
                if isinstance(obj, dict):
                    if obj.get("type") == "image": img_data = obj.get("data", {}); return
                    for v in obj.values(): walk_img(v)
                elif isinstance(obj, list):
                    for it in obj: walk_img(it)
            walk_img(msg_data)

            if not img_data: return
            img_bytes = None
            url = img_data.get("url")
            file_name = img_data.get("file")

            async with aiohttp.ClientSession(trust_env=True) as session:
                if file_name:
                    if file_name.startswith("base64://"): img_bytes = base64.b64decode(file_name.replace("base64://", ""))
                    elif os.path.exists(file_name):
                        with open(file_name, "rb") as f: img_bytes = f.read()
                    else:
                        try:
                            info = await event.bot.call_action("get_image", file=file_name)
                            if isinstance(info, dict) and info.get("file"):
                                local_p = info["file"].replace("file://", "")
                                if local_p.startswith("base64://"): img_bytes = base64.b64decode(local_p.replace("base64://", ""))
                                elif os.path.exists(local_p):
                                    with open(local_p, "rb") as f: img_bytes = f.read()
                        except Exception: pass

                if not img_bytes and url and str(url).startswith("http"):
                    try:
                        async with session.get(url, headers={"User-Agent": self.ua}) as resp:
                            if resp.status == 200: img_bytes = await resp.read()
                    except Exception: pass

                if not img_bytes or len(img_bytes) < 2048:
                    yield event.plain_result("❌ 抓取到的图片太小或已失效，请重新发送。")
                    return

                if len(img_bytes) > 500 * 1024:
                    try:
                        img = Image.open(io.BytesIO(img_bytes))
                        if img.mode in ("RGBA", "P"): img = img.convert("RGB")
                        img.thumbnail((1080, 1080), Image.Resampling.LANCZOS)
                        out_io = io.BytesIO()
                        img.save(out_io, format="JPEG", quality=85)
                        img_bytes = out_io.getvalue()
                    except Exception:
                        yield event.plain_result("❌ 图片过大且压缩失败，请重试。")
                        return

                ext = ".jpg"; mime = "image/jpeg"
                if img_bytes.startswith(b'\x89PNG\r\n\x1a\n'): ext, mime = ".png", "image/png"
                elif img_bytes.startswith(b'RIFF') and img_bytes[8:12] == b'WEBP': ext, mime = ".webp", "image/webp"

                tmp_path = CACHE_DIR / f"{time.time_ns()}{ext}"
                with open(tmp_path, "wb") as f: f.write(img_bytes)

                yield event.plain_result(self.msg_start)
                img_res = None
                for attempt in range(3):
                    try:
                        form = aiohttp.FormData()
                        form.add_field("api_key", self.api_key)
                        with open(tmp_path, "rb") as f:
                            form.add_field("image", f, filename=f"image{ext}", content_type=mime)
                            async with session.post("https://serpapi.com/image", data=form) as resp:
                                if resp.status == 200:
                                    img_res = await resp.json(content_type=None)
                                    break
                    except Exception: await asyncio.sleep(2)

                image_id = img_res.get("image_id") if img_res else None
                if not image_id:
                    yield event.plain_result("❌ 未能获取 image_id。")
                    return

                async def do_search(search_type):
                    params = {"engine": "google_lens", "api_key": self.api_key, "image_id": image_id, "type": search_type, "safe": "off", "no_cache": "true", "hl": self.hl, "country": self.country}
                    try:
                        async with session.get("https://serpapi.com/search", params=params) as resp:
                            if resp.status == 200: return await resp.json(content_type=None)
                    except Exception: pass
                    return None

                all_candidates = []
                best_uid = None
                best_name = "未知"
                max_followers = -1
                has_x = False
                has_kemono = False

                res_json = await do_search("exact_matches")
                exact_matches = res_json.get("exact_matches", []) if res_json else []
                d1, rt1, k1 = self.extract_accounts(exact_matches[:50])
                
                if k1:
                    has_kemono = True
                    parts = k1[0].split("_")
                    if len(parts) >= 4:
                        domain, service, creator_id = parts[1], parts[2], parts[3]
                        try:
                            links_url = f"https://{domain}/api/v1/{service}/user/{creator_id}/links"
                            async with session.get(links_url, headers={"User-Agent": self.ua, "Accept": "text/css"}, timeout=10) as lresp:
                                if lresp.status == 200:
                                    ldata = await lresp.json(content_type=None)
                                    for item in ldata:
                                        pub_id = str(item.get("public_id", ""))
                                        if pub_id: best_uid = pub_id; has_x = True; break
                        except: pass
                        if not best_uid:
                            try:
                                page_url = f"https://{domain}/{service}/user/{creator_id}"
                                async with session.get(page_url, headers={"User-Agent": "Mozilla/5.0 Chrome/124.0.0.0", "Accept": "text/html"}, timeout=10) as presp:
                                    if presp.status == 200:
                                        html = await presp.text()
                                        m_fanbox = re.search(r'([a-zA-Z0-9_]+)\.fanbox\.cc', html)
                                        if m_fanbox: best_uid = m_fanbox.group(1); has_x = True
                            except: pass
                        if best_uid:
                            name, followers = await self.get_true_name_and_followers(session, best_uid)
                            try: followers = int(followers)
                            except: followers = 0
                            best_name = name if (name and name != "未知") else "未知"
                            max_followers = followers
                elif d1: all_candidates = d1; has_x = True
                elif rt1: all_candidates = rt1; has_x = True

                if not has_x and not best_uid and not has_kemono:
                    res_json_v = await do_search("visual_matches")
                    visual_matches = res_json_v.get("visual_matches", []) if res_json_v else []
                    d2, rt2, k2 = self.extract_accounts(visual_matches[:50])
                    if k2:
                        has_kemono = True
                        parts = k2[0].split("_")
                        if len(parts) >= 4:
                            domain, service, creator_id = parts[1], parts[2], parts[3]
                            # 2.1 API 查询
                            try:
                                links_url = f"https://{domain}/api/v1/{service}/user/{creator_id}/links"
                                async with session.get(links_url, headers={"User-Agent": self.ua, "Accept": "text/css"}, timeout=10) as lresp:
                                    if lresp.status == 200:
                                        ldata = await lresp.json(content_type=None)
                                        for item in ldata:
                                            pub_id = str(item.get("public_id", ""))
                                            if pub_id: best_uid = pub_id; has_x = True; break
                            except: pass
                            # 2.2 网页兜底
                            if not best_uid:
                                try:
                                    page_url = f"https://{domain}/{service}/user/{creator_id}"
                                    async with session.get(page_url, headers={"User-Agent": "Mozilla/5.0 Chrome/124.0.0.0", "Accept": "text/html"}, timeout=10) as presp:
                                        if presp.status == 200:
                                            html = await presp.text()
                                            m_fanbox = re.search(r'([a-zA-Z0-9_]+)\.fanbox\.cc', html)
                                            if m_fanbox: best_uid = m_fanbox.group(1); has_x = True
                                except: pass
                    elif d2: all_candidates = d2; has_x = True
                    elif rt2: all_candidates = rt2; has_x = True

                if not has_x and not best_uid:
                    all_matches = exact_matches[:]
                    if not all_matches and 'visual_matches' in locals(): all_matches = visual_matches[:]
                    titles = [m.get("title", "") for m in all_matches if isinstance(m, dict)]
                    from collections import Counter
                    keywords = []
                    
                    stop_words = ["twitter", "porn", "profile", "写真", "私房", "cos", "post", "nude", "leaks", "leaked", "photo", "photos", "picture", "pictures", "free", "onlyfans", "naked", "masterfap", "fanbo", "fanbox", "pixiv", "from", "墙", "leaked", "onlyfans", "collection", "hentai"]
                    
                    for t in titles:
                        t = re.sub(r'(?i)\bRT\s*@[a-zA-Z0-9_]+:?', '', t)
                        t = re.sub(r'(?i)@[a-zA-Z0-9_]+', '', t)
                        t = re.sub(r'\[.*?\]|\(.*?\)', '', t) 
                        t = re.sub(r'[\u0400-\u04FF]+', '', t) 
                        t = re.sub(r'(?i)\s*-?\s*(COS团|COS君|Cosjun|私房猫|Sifangmao|秀人网|Fapello|Fapeza|MasterFap|Kemono|Sotwe|TwStalker)\s*', '', t)
                        
                        # 清理 "Post" 这种词
                        t = re.sub(r'(?i)^Post\s*', '', t)

                        # 1. 优先提取 Kemono 格式的 by "xxx"
                        km_match = re.search(r'by\s*"([^"]+)"', t, re.I) or re.search(r"by\s*'([^']+)'", t, re.I)
                        if km_match:
                            t = km_match.group(1)
                        else:
                            # 2. 针对带斜杠的：fliosofem / shibachipipa
                            if "/" in t:
                                parts = t.split("/")
                                for p in parts:
                                    p = p.strip()
                                    if len(p) >= 3:
                                        words = re.findall(r'[a-zA-Z0-9_\u4e00-\u9fa5\. ]{3,30}', p)
                                        for w in words:
                                            w = w.strip()
                                            if len(w) >= 3 and not any(bad in w.lower() for bad in stop_words):
                                                keywords.append(w)
                                continue
                                
                            # 3. 普通按破折号切分
                            t = re.split(r'\s*[-|–—]\s*', t)[0].strip()
                            t = t.replace('"', ' ').replace("'", " ")
                            
                        if not any(bad in t.lower() for bad in stop_words):
                            words = re.findall(r'[a-zA-Z0-9_\u4e00-\u9fa5\. ]{2,30}', t)
                            for w in words:
                                w = w.strip()
                                if len(w) >= 3 and not any(bad in w.lower() for bad in stop_words): 
                                    keywords.append(w)
                                    
                    counter = Counter(keywords)
                    top_words = [word for word, count in counter.most_common(3)]
                    logger.info(f"【高频反推】精准提取的候选词: {top_words}")
                    
                    # 3. 搜索逻辑：加入拆分降级策略
                    for word in top_words:
                        # 生成搜索变体：全名、去掉下划线、拆分中英文
                        search_variants = [word]
                        if "_" in word:
                            search_variants.append(word.replace("_", ""))
                            search_variants.extend(word.split("_"))
                        else:
                            en_part = re.findall(r'[a-zA-Z0-9]+', word)
                            cn_part = re.findall(r'[\u4e00-\u9fa5]+', word)
                            if en_part and cn_part:
                                search_variants.append(f"{cn_part[0]} {en_part[0]}")
                                search_variants.extend(en_part)
                                search_variants.extend(cn_part)
                        search_variants = list(dict.fromkeys([v.strip() for v in search_variants if len(v.strip()) >= 3]))

                        for variant in search_variants:
                            logger.info(f"【高频反推】尝试搜索变体: '{variant}'")
                            search_q = f'"{variant}" twitter OR x.com'
                            params = {"engine": "google", "q": search_q, "api_key": self.api_key}
                            try:
                                async with session.get("https://serpapi.com/search", params=params) as sresp:
                                    if sresp.status == 200:
                                        sdata = await sresp.json(content_type=None)
                                        for org in sdata.get("organic_results", [])[:10]:
                                            link = org.get("link", "")
                                            if "x.com" in link or "twitter.com" in link:
                                                m = re.search(r'(?:x\.com|twitter\.com)/([A-Za-z0-9_]+)', link)
                                                if m:
                                                    check_uid = m.group(1)
                                                    if check_uid.lower() in ["home", "search", "hashtag", "intent", "settings", "i"]: continue
                                                    name, followers = await self.get_true_name_and_followers(session, check_uid)
                                                    try: followers = int(followers)
                                                    except: followers = 0
                                                    if followers > 0:
                                                        if followers > max_followers: 
                                                            max_followers = followers
                                                            best_uid = check_uid
                                                            best_name = name
                                                        break 
                            except Exception: pass
                            if best_uid: break
                        if best_uid: break

                if all_candidates:
                    unique_cands = list(dict.fromkeys(all_candidates))
                    for uid in unique_cands[:5]:
                        name, followers = await self.get_true_name_and_followers(session, uid)
                        try: followers = int(followers)
                        except: followers = 0
                        if followers > max_followers: max_followers = followers; best_uid = uid; best_name = name
                    if not best_uid and unique_cands: best_uid = unique_cands[0]; best_name = "未知"

                if best_uid and (best_name == "未知" or not best_name):
                    name, _ = await self.get_true_name_and_followers(session, best_uid)
                    if name and name != "未知": best_name = name

                reply_msg = ""
                if best_uid:
                    reply_msg += f"🎯 **查询结果**\n\n🌐 平台：X (Twitter)\n"
                    if best_name and best_name != "未知": reply_msg += f"👤 名字：{best_name}\n"
                    reply_msg += f"🆔 账号：@{best_uid}\n🔗 主页：https://x.com/{best_uid}\n"
                    if self.enable_look_pic and (not is_group or group_id not in self.look_pic_blacklist):
                        self.look_pic_auth[(group_id, user_id)] = {"uid": best_uid, "time": time.time()}
                        reply_msg += f"\n💡 提示：回复「{self.look_cmds[0]}」并引用本条消息，可获取近期的图片哦~"
                    reply_msg += "\n⚠️ 识图存在误差，请自行核对哦~"
                else:
                    reply_msg = self.msg_not_found
                yield event.plain_result(reply_msg)

        except Exception as e:
            logger.error(f"查询失败: {type(e).__name__} - {e}")
            yield event.plain_result(self.msg_error)
        finally:
            if tmp_path and tmp_path.exists():
                try: tmp_path.unlink()
                except: pass
            now = time.time()
            for f in CACHE_DIR.glob("*"):
                if f.is_file() and (now - f.stat().st_mtime > 3600):
                    try: f.unlink()
                    except: pass
