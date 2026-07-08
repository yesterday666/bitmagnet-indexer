"""
qBittorrent API 客户端
=======================
封装与 qBittorrent Web API 的交互：
- 登录 / 登出
- 添加磁力链接（带分类标签）
- 查询种子列表及状态
- 删除种子（可选删除文件）
"""

import requests
import time

import config
from retry import retry_with_backoff
from logger_setup import log


class QBitClient:
    """qBittorrent Web API 客户端。"""

    def __init__(self):
        self.base = f"http://{config.QB_HOST}:{config.QB_PORT}/api/v2"
        self.session = requests.Session()
        self._logged_in = False
        self._pushed: set = set()
        self._last_health_ts = 0
        self._last_health_ok = False

    def reconfigure(self):
        """根据当前 config.QB_* 重建连接（改了主机/端口/账号后调用）。"""
        self.base = f"http://{config.QB_HOST}:{config.QB_PORT}/api/v2"
        self.session = requests.Session()
        self._logged_in = False
        self._last_health_ts = 0
        self._last_health_ok = False

    # ── 登录 ────────────────────────────────────────────────────

    def login(self) -> bool:
        """登录 qBittorrent，失败时自动重试。"""
        try:
            resp = self.session.post(
                f"{self.base}/auth/login",
                data={
                    "username": config.QB_USER,
                    "password": config.QB_PASSWORD,
                },
                timeout=15,
            )
            if resp.text == "Ok." or resp.status_code == 200:
                self._logged_in = True
                log.info("qBittorrent 登录成功 (%s:%d)", config.QB_HOST, config.QB_PORT)
                return True
            else:
                log.error("qBittorrent 登录失败: %s", resp.text)
                return False
        except Exception as e:
            log.error("qBittorrent 登录异常: %s", e)
            return False

    def ensure_login(self):
        """确保已登录，否则重新登录。"""
        if not self._logged_in:
            self.login()

    # ── 添加种子 ────────────────────────────────────────────────

    @retry_with_backoff(retry_on=(requests.exceptions.Timeout,
                                   requests.exceptions.ConnectionError))
    def add_magnet(self, info_hash: str, name: str,
                   category: str = None, save_path: str = None) -> dict:
        """
        通过磁力链接添加下载任务。

        Args:
            info_hash: 种子 info_hash（40 位 hex）
            name: 种子名称
            category: 分类标签
            save_path: 保存路径

        Returns:
            {"success": bool, "message": str}
        """
        self.ensure_login()

        magnet_uri = f"magnet:?xt=urn:btih:{info_hash}&dn={name}"
        data = {"urls": magnet_uri}

        if category or config.QB_CATEGORY:
            data["category"] = category or config.QB_CATEGORY
        if save_path or config.QB_SAVE_PATH:
            data["savepath"] = save_path or config.QB_SAVE_PATH
        data["paused"] = "false"

        resp = self.session.post(f"{self.base}/torrents/add", data=data, timeout=30)

        txt = (resp.text or "").strip()
        ok = False
        already = False
        if resp.status_code == 200:
            if txt == "" or "Ok." in txt:
                ok = True
            elif "Fails." in txt:
                ok = False
            else:
                try:
                    j = resp.json()
                    ok = (j.get("success_count", 0) or 0) > 0 or bool(j.get("added_torrent_ids")) or (j.get("pending_count", 0) or 0) > 0
                except Exception:
                    ok = ("added_torrent_ids" in txt) and ('"success_count":0' not in txt.replace(" ", ""))
        elif resp.status_code == 409 or "conflict" in txt.lower() or "already" in txt.lower():
            ok = True
            already = True

        if ok:
            self._pushed.add(info_hash)
            log.info("%s qBittorrent: %s | %s", "已在列表" if already else "已推送到", info_hash[:12], name[:80])
            return {"success": True, "message": "已在下载列表" if already else "已添加"}
        else:
            log.warning("qBittorrent 添加失败: status=%d text=%s", resp.status_code, txt[:200])
            return {"success": False, "message": txt[:200] or f"HTTP {resp.status_code}"}

    # ── 查询种子 ────────────────────────────────────────────────

    @retry_with_backoff(retry_on=(requests.exceptions.Timeout,
                                   requests.exceptions.ConnectionError))
    def get_torrents(self, category: str = None, hashes: list = None) -> list:
        """
        获取种子列表。

        Args:
            category: 按分类过滤
            hashes: 按 info_hash 列表过滤

        Returns:
            种子信息列表
        """
        self.ensure_login()

        params = {}
        if category:
            params["category"] = category
        if hashes:
            params["hashes"] = "|".join(hashes)

        resp = self.session.get(f"{self.base}/torrents/info", params=params, timeout=15)
        resp.raise_for_status()
        return resp.json()

    # ── 删除种子 ────────────────────────────────────────────────

    @retry_with_backoff(retry_on=(requests.exceptions.Timeout,
                                   requests.exceptions.ConnectionError))
    def delete_torrents(self, hashes: list, delete_files: bool = True) -> bool:
        """
        删除种子任务。

        Args:
            hashes: info_hash 列表
            delete_files: 是否同时删除本地文件

        Returns:
            是否成功
        """
        self.ensure_login()

        data = {"hashes": "|".join(hashes), "deleteFiles": str(delete_files).lower()}
        resp = self.session.post(f"{self.base}/torrents/delete", data=data, timeout=15)

        if resp.status_code == 200:
            log.info("已删除种子: %s (delete_files=%s)", ", ".join(h[:12] for h in hashes), delete_files)
            return True
        log.warning("删除种子失败: %s", resp.text[:200])
        return False

    # ── 健康检查 ────────────────────────────────────────────────

    def health_check(self) -> bool:
        """快速健康检查（10秒缓存）。"""
        now = time.time()
        if now - self._last_health_ts < 10:
            return self._last_health_ok
        self._last_health_ts = now
        try:
            self.ensure_login()
            resp = self.session.get(f"{self.base}/app/version", timeout=3)
            self._last_health_ok = resp.status_code == 200
            return self._last_health_ok
        except Exception:
            self._last_health_ok = False
            return False


# 全局单例
qb = QBitClient()
