import base64, json, time, aiohttp, asyncio, re, os, random, io, shutil
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
        self.ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0"
        
        self.hl = "zh-cn"
        self.country = "at"
        self.safe = "off"
        
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
        # 完美兼容丰川祥子人设，不包含任何容易触发AI审核的脏字
        self.prompt_borrow = self._get_prompt_str(p_cfg, "prompt_borrow", "该用户试图白嫖别人查出来的照片。请结合丰川祥子高傲冷漠的客服人设，嘲讽他连找图都要蹭别人，是个满脑子只想着自己的伸手党。绝对不要使用脏字。")
        self.prompt_used = self._get_prompt_str(p_cfg, "prompt_used", "用户看完了还要继续索取。请结合丰川祥子阴阳怪气的语气，嘲讽他贪得无厌、欲求不满，这副贪心的嘴脸真难看。")
        self.prompt_expired = self._get_prompt_str(p_cfg, "prompt_expired", "用户超时未看。请用丰川祥子冷漠的语气，嘲讽他连看个图都磨磨唧唧，暗示他连自己的欲望都管理不好。")
        self.prompt_cooldown = self._get_prompt_str(p_cfg, "prompt_cooldown", "用户急躁连发。请用丰川祥子高傲的语气，嘲讽他急不可耐，毫无优雅可言。")
        self.prompt_group_disabled = self._get_prompt_str(p_cfg, "prompt_group_disabled", "在禁用群找图。请用丰川祥子冷漠的语气，告诉他规矩就是规矩，这里不是垃圾场，让他离开。")
        self.prompt_look_disabled = self._get_prompt_str(p_cfg, "prompt_look_disabled", "未开启看图。请结合丰川祥子高高在上的语气，冷漠地俯视他，暗示他满脑子都是下贱废料。")
        self.prompt_no_reply = self._get_prompt_str(p_cfg, "prompt_no_reply", "用户未引用原消息。请用丰川祥子冷漠的语气，嘲讽他连最基本的规则都不懂，缺乏常识。")

        # 祥子专属兜底金句（如果AI拉胯或者拒答，直接发这些）
        self.no_auth_msgs = [
            "贵安。真是高高在上呢，连自己动手的勇气都没有，只配捡别人剩下的图吗？",
            "你这个人，脑子里只想着自己呢。白嫖别人查出来的东西，不觉得可悲吗？",
            "连查图都要蹭别人的，你还能背负起什么？"
        ]
        self.already_used_msgs = [
            "贪得无厌也该有个限度吧？看过了还来要，真是难看的嘴脸。",
            "满脑子只想着自己的欲望，软弱的你真是没救了。",
            "贵安。看过了就请回吧，这里不欢迎欲求不满的人。"
        ]
        self.expired_msgs = [
            "连看个图都磨磨唧唧，活该错过。",
            "软弱的你已经死了。连这点时间都把握不住吗？",
            "贵安。超时了。你连管理自己欲望的能力都没有吗？"
        ]
        self.disabled_msgs = ["规矩就是规矩。这里不是垃圾桶。"]
        self.look_pic_msgs = ["满脑子都是下贱废料，真是不堪入目。"]
        self.no_reply_msgs = ["连最基本的引用规矩都不懂，缺乏教养。"]
        
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
                    text = resp.completion_text.strip()
                    # 🌟 防AI背刺机制：如果AI吐出了拒绝词汇，直接拦截，用祥子金句兜底！
                    if any(bad in text for bad in ["不能", "抱歉", "无法", "对不起", "违规", "拒绝", "作为", "不能帮"]):
                        return random.choice(default_msgs)
                    return text
        except Exception as e:
            logger.error(f"【AI模型】生成嘲讽失败: {e}")
        return random.choice(default_msgs)

    async def recall_msg_later(self, bot, message_id, delay):
        if delay <= 0: return
        await asyncio.sleep(delay)
        try:
            await bot.call_action("delete_msg", message_id=message_id)
        except Exception: pass

    def extract_pools(self, matches):
        abs_ids = set()
        priority_names = set()
        kemono_links = set()
        rt_ids = set() 
        
        exclude_sites = ["thefap.net", "kaizty", "misskon.com", "deviantart.com", "thotsbay.com"]
        
        for m in matches or []:
            if not isinstance(m, dict): continue
            title = str(m.get("title", ""))
            link = str(m.get("link", ""))
            
            if re.search(r"(?i)\bReplies by\b", title): continue
            if any(site in link.lower() for site in exclude_sites): continue

            km = re.search(r"https?://(kemono\.[a-z]+)/([a-z0-9]+)/user/(\d+)", link, re.I)
            if km:
                kemono_links.add(km.groups())
                by_m = re.search(r'by\s*"([^"]+)"', title, re.I)
                if by_m:
                    name = by_m.group(1).strip()
                    if len(name) >= 2: priority_names.add(name)
                continue

            if any(site in link.lower() for site in ["fapello.com", "fapeza.com", "masterfap.net"]):
                slash = re.match(r"^([a-zA-Z0-9_]+)\s*/\s*([a-zA-Z0-9_]+)", title)
                if slash:
                    priority_names.add(slash.group(1).strip())
                    priority_names.add(slash.group(2).strip())
                else:
                    fw = re.match(r"^([a-zA-Z0-9_]+)", title)
                    if fw: priority_names.add(fw.group(1).strip())
                continue

            if any(s in link.lower() for s in ["sotwe.com", "twstalker.com", "instalker.com"]):
                lm = re.search(r'https?://(?:www\.)?(?:sotwe|twstalker|instalker)\.com/([A-Za-z0-9_]+)', link, re.I)
                if lm: abs_ids.add(lm.group(1))
                pm = re.search(r"\(@([A-Za-z0-9_]{1,15})\)", title)
                if pm: abs_ids.add(pm.group(1))
                continue

            if re.search(r"(?i)(?:x\.com|twitter\.com)", link):
                lm = re.search(r"(?i)(?:x\.com|twitter\.com)/([A-Za-z0-9_]{1,15})", link)
                if lm and lm.group(1).lower() not in ["home", "search", "hashtag", "intent", "settings", "i", "share", "login"]:
                    abs_ids.add(lm.group(1))
                continue

            is_rt = bool(re.search(r"(?i)\bRT\b|بواسطة|repost|转发", title))
            if is_rt:
                for rid in re.findall(r"(?i)\bRT\s*@([A-Za-z0-9_]{1,15})\b", title):
                    rt_ids.add(rid)
                for pm in re.findall(r"\(@([A-Za-z0-9_]{1,15})\)", title):
                    rt_ids.add(pm)
                continue

            pm = re.search(r"\(@([A-Za-z0-9_]{1,15})\)", title)
            if pm: abs_ids.add(pm.group(1))

            om = re.search(r"^(.+?)\s+on\s+X", title, re.I)
            if om:
                name = re.sub(r'\(@[^)]+\)', '', om.group(1)).strip()
                name = re.sub(r'[^a-zA-Z0-9\u4e00-\u9fa5_]', '', name)
                if len(name) >= 2: priority_names.add(name)

        return list(abs_ids), list(priority_names), list(kemono_links), list(rt_ids)

    async def get_fx(self, session, uid):
        if not uid: return "未知", 0
        for _ in range(2):
            try:
                async with session.get(f"https://api.fxtwitter.com/{uid}", headers={"User-Agent": self.ua}, timeout=10) as r:
                    if r.status == 200:
                        u = (await r.json()).get("user") or {}
                        return u.get("name", "未知"), int(u.get("followers", 0))
            except Exception: pass
            await asyncio.sleep(1)
        return "未知", 0

    def check_valid_account(self, uid, name, f):
        if f <= 0: return False
        if any(bad in name.lower() for bad in ["account +", "http", "sotwe", "leaks", "fapello", "🔞"]): return False
        return True

    async def resolve_kemono(self, session, k_list):
        accounts = []
        for domain, service, creator_id in k_list[:5]:
            kemono_id = None
            try:
                async with session.get(f"https://{domain}/api/v1/{service}/user/{creator_id}/links", headers={"User-Agent": self.ua, "Accept": "text/css"}, timeout=10) as r:
                    if r.status == 200:
                        data = await r.json(content_type=None)
                        for item in data:
                            if item.get("public_id"): kemono_id = str(item["public_id"]); break
            except: pass
            if not kemono_id:
                try:
                    async with session.get(f"https://{domain}/{service}/user/{creator_id}", headers={"User-Agent": self.ua, "Accept": "text/html"}, timeout=10) as r:
                        if r.status == 200:
                            html = await r.text()
                            m = re.search(r'([a-zA-Z0-9_]+)\.fanbox\.cc', html)
                            if m: kemono_id = m.group(1)
                except: pass
                
            if kemono_id:
                name, f = await self.get_fx(session, kemono_id)
                if self.check_valid_account(kemono_id, name, f):
                    accounts.append({"uid": kemono_id, "name": name, "f": f})
        return accounts

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
                reply_data = await event.bot.call_action("get_msg", message_id=int(reply_id))
                match = re.search(r"@([A-Za-z0-9_]{1,15})", json.dumps(reply_data, ensure_ascii=False))
                if not match: return
                uid = match.group(1)
                
                auth_key = (group_id, user_id)
                current_time = time.time()
                
                for k in list(self.look_pic_auth.keys()):
                    if current_time - self.look_pic_auth[k]["time"] > self.look_pic_timeout + 3600:
                        del self.look_pic_auth[k]

                record = self.look_pic_auth.get(auth_key)

                if record:
                    if record["uid"] != uid:
                        yield event.plain_result("❌ 这与你刚才查询的账号不符，请引用正确的消息。")
                        return
                    if record.get("viewed"):
                        yield event.plain_result(await self.get_smart_roast(event, self.already_used_msgs, self.prompt_used))
                        return
                    if current_time - record["time"] > self.look_pic_timeout:
                        yield event.plain_result(await self.get_smart_roast(event, self.expired_msgs, self.prompt_expired))
                        return
                    
                    record["viewed"] = True
                else:
                    is_others = False
                    for k, v in self.look_pic_auth.items():
                        if v["uid"] == uid and k[0] == group_id:
                            is_others = True
                            break
                    if is_others:
                        yield event.plain_result(await self.get_smart_roast(event, self.no_auth_msgs, self.prompt_borrow))
                    else:
                        yield event.plain_result(await self.get_smart_roast(event, self.already_used_msgs, self.prompt_used))
                    return

                yield event.plain_result("🔍 正在提取图片...")
                async with aiohttp.ClientSession(trust_env=True) as session:
                    async with session.get(f"https://api.fxtwitter.com/2/profile/{uid}/media", headers={"User-Agent": self.ua}) as r:
                        if r.status != 200: return
                        data = await r.json()
                        img_urls = []
                        for tw in data.get("results", []):
                            for p in tw.get("media", {}).get("photos", []):
                                if len(img_urls) < 10 and p.get("url"): img_urls.append(p.get("url"))
                        if not img_urls:
                            yield event.plain_result("❌ 该账号近期没有图片")
                            return
                        b64_list = []
                        for u in img_urls:
                            async with session.get(u, headers={"User-Agent": self.ua}) as ir:
                                if ir.status == 200: b64_list.append(f"base64://{base64.b64encode(await ir.read()).decode('utf-8')}")
                        try:
                            info = await event.bot.call_action("get_login_info")
                            bot_qq = int(info.get("user_id", 10000))
                            bot_name = info.get("nickname", "小祥")
                        except Exception:
                            bot_qq = getattr(event.message_obj, "self_id", 10000)
                            bot_name = "小祥"

                        nodes = [{"type": "node", "data": {"user_id": bot_qq, "nickname": bot_name, "content": [{"type": "image", "data": {"file": b}}]}} for b in b64_list]
                        nodes.append({"type": "node", "data": {"user_id": bot_qq, "nickname": bot_name, "content": [{"type": "text", "data": {"text": "⚠️ 提示：请自行核对是否为本人。"}}]}})
                        ret = None
                        if is_group: ret = await event.bot.call_action("send_group_forward_msg", group_id=int(group_id), messages=nodes)
                        else: ret = await event.bot.call_action("send_private_forward_msg", user_id=int(user_id), messages=nodes)
                        if self.auto_recall_time > 0 and isinstance(ret, dict) and ret.get("message_id"):
                            asyncio.create_task(self.recall_msg_later(event.bot, int(ret["message_id"]), self.auto_recall_time))
            except Exception as e: logger.error(f"看图错误: {e}")
            return

        # ================= 找鸡功能 =================
        if msg not in self.commands: return
        if is_group and group_id in self.disabled_groups:
            yield event.plain_result(await self.get_smart_roast(event, self.disabled_msgs, self.prompt_group_disabled))
            return

        now_time = time.time()
        if is_group:
            last_time = self.search_cooldown.get(group_id, 0)
            if now_time - last_time < self.search_cooldown_time:
                yield event.plain_result(await self.get_smart_roast(event, ["急什么？赶着投胎吗？"], self.prompt_cooldown))
                return
            self.search_cooldown[group_id] = now_time

        if not self.api_key:
            yield event.plain_result("❌ 未配置 SerpApi API Key。")
            return
        
        raw_msg = event.message_obj.raw_message.get("message", [])
        segs = json.loads(raw_msg) if isinstance(raw_msg, str) else raw_msg
        reply_id = next((x.get("data", {}).get("id") for x in segs if isinstance(x, dict) and x.get("type") == "reply"), None)
        if not reply_id:
            yield event.plain_result(await self.get_smart_roast(event, self.no_reply_msgs, self.prompt_no_reply))
            return
        
        yield event.plain_result("🔍 正在查询...")
        
        try:
            msg_data = await event.bot.call_action("get_msg", message_id=int(reply_id))
            url = None
            def walk(o):
                nonlocal url
                if isinstance(o, dict):
                    if o.get("type") == "image": url = o.get("data", {}).get("url")
                    for v in o.values(): walk(v)
                elif isinstance(o, list):
                    for i in o: walk(i)
            walk(msg_data)
            if not url: return

            async with aiohttp.ClientSession(trust_env=True) as session:
                async with session.get(url) as r: img_bytes = await r.read()
                form = aiohttp.FormData()
                form.add_field("api_key", self.api_key)
                form.add_field("image", img_bytes, filename="img.jpg", content_type="image/jpeg")
                try:
                    async with session.post("https://serpapi.com/image", data=form, timeout=15) as r:
                        img_id = (await r.json()).get("image_id")
                        if not img_id: 
                            yield event.plain_result("❌ 识图失败")
                            return
                except Exception as e:
                    yield event.plain_result("❌ 连接 SerpApi 失败！")
                    return

                base_params = {"engine": "google_lens", "api_key": self.api_key, "image_id": img_id, "hl": self.hl, "country": self.country, "safe": self.safe, "auto_crop": "false"}
                
                async def process_step(m_type):
                    try:
                        async with session.get("https://serpapi.com/search", params={**base_params, "type": m_type}, timeout=20) as r:
                            a_ids, p_names, k_links, r_ids = self.extract_pools((await r.json(content_type=None)).get(m_type, []))
                    except Exception: return []

                    accs = []
                    
                    for uid in a_ids[:10]:
                        name, f = await self.get_fx(session, uid)
                        if self.check_valid_account(uid, name, f):
                            accs.append({"uid": uid, "name": name, "f": f})
                    if accs: return accs
                            
                    if k_links:
                        k_accs = await self.resolve_kemono(session, k_links)
                        if k_accs: return k_accs

                    for name in p_names[:5]:
                        if any(bad in name.lower() for bad in ["account", "http", "sotwe", "leaks", "fapello", "nude"]): continue
                        fx_name, f1 = await self.get_fx(session, name)
                        if self.check_valid_account(name, fx_name, f1):
                            accs.append({"uid": name, "name": fx_name, "f": f1})
                            return accs
                        try:
                            async with session.get("https://serpapi.com/search", params={"engine": "google", "q": f'"{name}" site:x.com OR site:twitter.com', "api_key": self.api_key}, timeout=15) as sresp:
                                if sresp.status == 200:
                                    for org in (await sresp.json(content_type=None)).get("organic_results", [])[:3]:
                                        link = org.get("link", "")
                                        if "x.com" in link or "twitter.com" in link:
                                            m = re.search(r'(?:x\.com|twitter\.com)/([A-Za-z0-9_]+)', link)
                                            if m and m.group(1).lower() not in ["home", "search", "hashtag", "intent", "settings", "i"]:
                                                real_name, real_f = await self.get_fx(session, m.group(1))
                                                if self.check_valid_account(m.group(1), real_name, real_f):
                                                    accs.append({"uid": m.group(1), "name": real_name, "f": real_f})
                                                    return accs
                        except Exception: pass

                    for uid in r_ids[:5]:
                        name, f = await self.get_fx(session, uid)
                        if self.check_valid_account(uid, name, f):
                            accs.append({"uid": uid, "name": name, "f": f})
                            
                    return accs

                all_valid_accounts = await process_step("exact_matches")
                if not all_valid_accounts:
                    all_valid_accounts = await process_step("visual_matches")

                if all_valid_accounts:
                    unique_accounts = {}
                    for acc in all_valid_accounts:
                        if acc["uid"] not in unique_accounts or acc["f"] > unique_accounts[acc["uid"]]["f"]:
                            unique_accounts[acc["uid"]] = acc
                    
                    best_acc = max(unique_accounts.values(), key=lambda x: x["f"])
                    clean_name = re.sub(r'https?://\S+', '', best_acc['name'])
                    clean_name = re.sub(r'(?i)account\s*\+?', '', clean_name)
                    clean_name = clean_name.replace('|', ' ').replace('✨', '').strip()
                    clean_name = re.sub(r'\s+', ' ', clean_name)
                    
                    reply_msg = f"🎯 **查询结果**\n🌐 平台：X\n👤 名字：{clean_name}\n🆔 账号：@{best_acc['uid']}\n🔗 主页：https://x.com/{best_acc['uid']}\n"
                    if self.enable_look_pic:
                        self.look_pic_auth[(group_id, user_id)] = {"uid": best_acc['uid'], "time": time.time(), "viewed": False}
                        reply_msg += f"\n💡 提示：回复「{self.look_cmds[0]}」并引用本条消息，可获取图片。"
                    reply_msg += "\n⚠️ 识图存在误差，请自行核对哦~"
                    yield event.plain_result(reply_msg)
                else:
                    yield event.plain_result("❌ 未能找到有效账号")
        except Exception as e: 
            logger.error(f"终极报错: {e}")
            yield event.plain_result("❌ 处理过程中发生异常。")
