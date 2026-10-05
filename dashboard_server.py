# -*- coding: utf-8 -*-
"""
FreeFire Level Up Bot - Professional Web Dashboard & Real-Time EXP Tracker
Embedded Async Web Server (aiohttp)

Supports:
  - BR (Battle Royale) / LW (Lone Wolf) dual-engine mode display
  - Level-based AUTO mode routing (Lv1-2 -> BR, Lv3+ -> LW)
  - Per-account Pause / Resume
  - Per-account Mode Override (AUTO / BR / LW)
  - Per-account Restart
"""

import asyncio
import json
import os
import time
from typing import Dict, List, Any, Optional
from aiohttp import web

VALID_MODE_OVERRIDES = ("AUTO", "BR", "LW")


# Global bot state shared between Main.py and Web Dashboard
class BotState:
    def __init__(self):
        self.accounts: Dict[str, Dict[str, Any]] = {}
        self.logs: List[Dict[str, Any]] = []
        self.max_logs = 200
        self.total_matches = 0
        self.total_matches_started = 0
        self.total_gained_exp = 0
        self.start_time = time.time()
        self.account_workers: Dict[str, asyncio.Task] = {}
        self.refresh_callbacks: Dict[str, Any] = {}
        self.account_credentials: Dict[str, Dict[str, Any]] = {}

        # ---- Dual-engine (BR/LW) state ----
        self.paused_accounts: set = set()
        self.account_writers: Dict[str, List[Any]] = {}
        self.auth_to_game_id: Dict[str, str] = {}
        self.game_to_auth_id: Dict[str, str] = {}
        self.account_token_map: Dict[str, str] = {}
        self.mode_overrides: Dict[str, str] = {}   # uid -> "AUTO" | "BR" | "LW"

    # ==================== LOGGING ====================
    def log(self, message: str, level: str = "info", uid: Optional[str] = None):
        entry = {
            "time": time.strftime("%H:%M:%S"),
            "level": level,
            "message": message,
            "uid": uid
        }
        self.logs.append(entry)
        if len(self.logs) > self.max_logs:
            self.logs.pop(0)

    # ==================== ACCOUNTS ====================
    def register_account(self, uid: str, nickname: str, region: str, level: int, exp: int, likes: int = 0):
        uid_str = str(uid)
        if uid_str not in self.accounts:
            self.accounts[uid_str] = {
                "uid": uid_str,
                "nickname": nickname or f"Player_{uid_str[:6]}",
                "region": region or "BD",
                "level": level or 1,
                "initial_exp": exp,
                "current_exp": exp,
                "gained_exp": 0,
                "likes": likes or 0,
                "status": "ONLINE",
                "mode": None,                      # currently running engine: "BR" | "LW" | None
                "matches_played": 0,
                "br_matches": 0,
                "lw_matches": 0,
                "active_matches": 0,
                "last_match_time": None,
                "last_updated": time.strftime("%H:%M:%S")
            }
        else:
            acc = self.accounts[uid_str]
            if nickname:
                acc["nickname"] = nickname
            if region:
                acc["region"] = region
            if level:
                acc["level"] = level
            acc["current_exp"] = exp
            acc["gained_exp"] = max(0, exp - acc["initial_exp"])
            acc["likes"] = likes
            acc["status"] = "ONLINE"
            acc["last_updated"] = time.strftime("%H:%M:%S")
        self.recalc_totals()

    def update_exp(self, uid: str, current_exp: int, level: Optional[int] = None):
        uid_str = str(uid)
        if uid_str in self.accounts:
            acc = self.accounts[uid_str]
            old_exp = acc["current_exp"]
            old_level = acc.get("level", 1)
            acc["current_exp"] = current_exp
            if level is not None and level > 0:
                acc["level"] = level
            acc["gained_exp"] = max(0, current_exp - acc["initial_exp"])
            acc["last_updated"] = time.strftime("%H:%M:%S")
            diff = current_exp - old_exp
            if diff > 0:
                self.log(f"Account {acc['nickname']} ({uid_str}) gained +{diff} EXP! Total Gained: +{acc['gained_exp']}", "success", uid_str)
            if level is not None and old_level and level > old_level:
                self.log(f"Account {acc['nickname']} ({uid_str}) LEVELED UP: Lv {old_level} -> Lv {level}!", "success", uid_str)
            self.recalc_totals()

    def update_status(self, uid: str, status: str, active_matches: Optional[int] = None):
        uid_str = str(uid)
        if uid_str in self.accounts:
            self.accounts[uid_str]["status"] = status
            if active_matches is not None:
                self.accounts[uid_str]["active_matches"] = active_matches
            self.accounts[uid_str]["last_updated"] = time.strftime("%H:%M:%S")

    def increment_match(self, uid: str):
        uid_str = str(uid)
        self.total_matches += 1
        if uid_str in self.accounts:
            acc = self.accounts[uid_str]
            acc["matches_played"] += 1
            mode = acc.get("mode")
            if mode == "BR":
                acc["br_matches"] = acc.get("br_matches", 0) + 1
            elif mode == "LW":
                acc["lw_matches"] = acc.get("lw_matches", 0) + 1
            acc["last_match_time"] = time.strftime("%H:%M:%S")
            acc["last_updated"] = time.strftime("%H:%M:%S")
            self.log(f"Account {acc['nickname']} finished Match #{acc['matches_played']}", "info", uid_str)

    def increment_match_started(self):
        self.total_matches_started += 1

    def recalc_totals(self):
        self.total_gained_exp = sum(acc.get("gained_exp", 0) for acc in self.accounts.values())

    # ==================== PAUSE / RESUME ====================
    def is_paused(self, uid: str) -> bool:
        return str(uid) in self.paused_accounts

    def set_paused(self, uid: str, paused: bool):
        uid_str = str(uid)
        if paused:
            self.paused_accounts.add(uid_str)
            self.update_status(uid_str, "PAUSED", 0)
            self.log(f"Account {uid_str} PAUSED by user.", "warning", uid_str)
        else:
            self.paused_accounts.discard(uid_str)
            self.update_status(uid_str, "ONLINE")
            self.log(f"Account {uid_str} RESUMED by user.", "success", uid_str)

    # ==================== WRITER REGISTRY ====================
    def register_writer(self, uid: str, writer: Any):
        uid_str = str(uid)
        if not uid_str:
            return
        try:
            self.account_writers.setdefault(uid_str, [])
            if writer not in self.account_writers[uid_str]:
                self.account_writers[uid_str].append(writer)
        except Exception:
            pass

    def unregister_writer(self, uid: str, writer: Any):
        uid_str = str(uid)
        try:
            lst = self.account_writers.get(uid_str)
            if lst and writer in lst:
                lst.remove(writer)
            if lst is not None and not lst:
                self.account_writers.pop(uid_str, None)
        except Exception:
            pass

    def close_writers_for_account(self, uid: str):
        uid_str = str(uid)
        for writer in list(self.account_writers.get(uid_str, [])):
            try:
                if writer and not writer.is_closing():
                    writer.close()
            except Exception:
                pass

    # ==================== MODE CONTROL ====================
    def get_mode_override(self, uid: str) -> str:
        return self.mode_overrides.get(str(uid), "AUTO")

    def set_mode_override(self, uid: str, mode: str) -> bool:
        uid_str = str(uid)
        mode = str(mode or "AUTO").upper()
        if mode not in VALID_MODE_OVERRIDES:
            return False
        old = self.mode_overrides.get(uid_str, "AUTO")
        if mode == "AUTO":
            self.mode_overrides.pop(uid_str, None)
        else:
            self.mode_overrides[uid_str] = mode
        if old != mode:
            self.log(f"Account {uid_str} mode override: {old} -> {mode}", "warning", uid_str)
        return True

    def set_account_mode(self, uid: str, mode: Optional[str]):
        uid_str = str(uid)
        if uid_str in self.accounts:
            self.accounts[uid_str]["mode"] = mode
            self.accounts[uid_str]["last_updated"] = time.strftime("%H:%M:%S")

    # ==================== ALIAS RESOLUTION ====================
    def resolve_uids(self, uid: str) -> set:
        uid_str = str(uid)
        resolved = {uid_str}
        if uid_str in self.game_to_auth_id:
            resolved.add(str(self.game_to_auth_id[uid_str]))
        if uid_str in self.auth_to_game_id:
            resolved.add(str(self.auth_to_game_id[uid_str]))
        return resolved


bot_state = BotState()


# ==================== HTTP HANDLERS ====================

TEMPLATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates", "index.html")

async def handle_index(request: web.Request) -> web.Response:
    if os.path.exists(TEMPLATE_PATH):
        with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
            content = f.read()
    else:
        content = "<h1>templates/index.html not found!</h1>"
    return web.Response(text=content, content_type="text/html", charset="utf-8")


async def handle_get_stats(request: web.Request) -> web.Response:
    accounts_data = []
    for acc in bot_state.accounts.values():
        a = dict(acc)
        uid_str = str(a.get("uid"))
        a["paused"] = bot_state.is_paused(uid_str)
        a["mode_override"] = bot_state.get_mode_override(uid_str)
        accounts_data.append(a)
    accounts_data.sort(key=lambda x: x.get("gained_exp", 0), reverse=True)
    return web.json_response({
        "total_accounts": len(bot_state.accounts),
        "total_matches": bot_state.total_matches,
        "total_matches_started": bot_state.total_matches_started,
        "total_gained_exp": bot_state.total_gained_exp,
        "accounts": accounts_data,
        "logs": bot_state.logs[-60:],
        "uptime": int(time.time() - bot_state.start_time)
    })


async def handle_add_account(request: web.Request) -> web.Response:
    try:
        data = await request.json()
        accounts_file = "accounts.json"
        existing = []
        if os.path.exists(accounts_file):
            try:
                with open(accounts_file, "r", encoding="utf-8") as f:
                    existing = json.load(f)
            except Exception:
                existing = []

        if "uid" in data and "password" in data:
            uid = str(data["uid"]).strip()
            pwd = str(data["password"]).strip()
            if not uid or not pwd:
                return web.json_response({"status": "error", "error": "UID and Password are required"})
            existing = [acc for acc in existing if str(acc.get("uid")) != uid]
            existing.append({"uid": uid, "password": pwd})
        elif "token" in data:
            token = str(data["token"]).strip()
            if not token:
                return web.json_response({"status": "error", "error": "Token is required"})
            existing = [acc for acc in existing if acc.get("token") != token]
            existing.append({"token": token})
        else:
            return web.json_response({"status": "error", "error": "Invalid payload"})

        with open(accounts_file, "w", encoding="utf-8") as f:
            json.dump(existing, f, indent=2)

        bot_state.log(f"New account added: {data.get('uid') or 'Token'}", "success")

        # Trigger dynamic worker launch
        if "on_account_added" in bot_state.refresh_callbacks:
            asyncio.create_task(bot_state.refresh_callbacks["on_account_added"](data))

        return web.json_response({"status": "ok"})
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)})


async def handle_delete_account(request: web.Request) -> web.Response:
    try:
        data = await request.json()
        uid = str(data.get("uid")).strip()
        resolved = bot_state.resolve_uids(uid)

        accounts_file = "accounts.json"
        if os.path.exists(accounts_file):
            with open(accounts_file, "r", encoding="utf-8") as f:
                existing = json.load(f)
            existing = [acc for acc in existing if str(acc.get("uid")) not in resolved]
            with open(accounts_file, "w", encoding="utf-8") as f:
                json.dump(existing, f, indent=2)

        for u in resolved:
            bot_state.close_writers_for_account(u)
            bot_state.paused_accounts.discard(u)
            bot_state.mode_overrides.pop(u, None)
            if u in bot_state.accounts:
                del bot_state.accounts[u]
            if u in bot_state.account_workers:
                try:
                    bot_state.account_workers[u].cancel()
                except Exception:
                    pass
                del bot_state.account_workers[u]

        if "on_account_deleted" in bot_state.refresh_callbacks:
            asyncio.create_task(bot_state.refresh_callbacks["on_account_deleted"](list(resolved)))

        bot_state.log(f"Account {uid} removed from rotation.", "warning", uid)
        return web.json_response({"status": "ok"})
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)})


async def handle_refresh_account(request: web.Request) -> web.Response:
    try:
        data = await request.json()
        uid = str(data.get("uid")).strip()
        if "on_refresh_account" in bot_state.refresh_callbacks:
            asyncio.create_task(bot_state.refresh_callbacks["on_refresh_account"](uid))
        return web.json_response({"status": "ok"})
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)})


async def handle_restart_account(request: web.Request) -> web.Response:
    try:
        data = await request.json()
        uid = str(data.get("uid")).strip()
        bot_state.set_paused(uid, False)
        bot_state.log(f"Restart requested for account {uid}.", "warning", uid)
        if "on_restart_account" in bot_state.refresh_callbacks:
            asyncio.create_task(bot_state.refresh_callbacks["on_restart_account"](uid))
        return web.json_response({"status": "ok"})
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)})


async def handle_pause_account(request: web.Request) -> web.Response:
    try:
        data = await request.json()
        uid = str(data.get("uid")).strip()
        paused = bool(data.get("paused", True))
        bot_state.set_paused(uid, paused)
        if "on_pause_toggle" in bot_state.refresh_callbacks:
            asyncio.create_task(bot_state.refresh_callbacks["on_pause_toggle"](uid, paused))
        return web.json_response({"status": "ok", "paused": paused})
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)})


async def handle_mode_account(request: web.Request) -> web.Response:
    try:
        data = await request.json()
        uid = str(data.get("uid")).strip()
        mode = str(data.get("mode", "AUTO")).upper()
        if not bot_state.set_mode_override(uid, mode):
            return web.json_response({"status": "error", "error": f"Invalid mode '{mode}'. Use AUTO, BR or LW."})
        return web.json_response({"status": "ok", "mode": mode})
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)})


async def start_web_dashboard(host: str = "0.0.0.0", port: int = 5000):
    app = web.Application()
    app.router.add_get("/", handle_index)
    app.router.add_get("/api/stats", handle_get_stats)
    app.router.add_post("/api/account/add", handle_add_account)
    app.router.add_post("/api/account/delete", handle_delete_account)
    app.router.add_post("/api/account/refresh", handle_refresh_account)
    app.router.add_post("/api/account/restart", handle_restart_account)
    app.router.add_post("/api/account/pause", handle_pause_account)
    app.router.add_post("/api/account/mode", handle_mode_account)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    print(f"\033[92m[+] Web Dashboard running on http://localhost:{port}\033[0m")
