#!/usr/bin/env python3
"""头像热更 CDN 探测（一次性工具，2026-10-05 定案 Phase A）。

【它回答的问题】
协议里头像只有**编号**（1004 ``playerInitClass.icon`` 等），图片本体在游戏
热更资源里（用户证实：Cherrytale Asset 是游戏本体解包，头像在热更目录）。
官方客户端登录就能显示头像 ⇒ 一定存在一条可下发的链路。本脚本把它摸出来：

    ① 握手（99015/99016，**不需要登录态**）→ cdnList / settingsNodeId / jsonInfo
    ② 真实登录一次（复用 ``main.py run login``，与日常使用同级别操作）
       → 1004 头像编号已由 ``tasks/login.py`` 落进账号库（``session_entry.avatar``）
    ③ GET ``/api/getMd5ByNode_V2.jsp?node={settingsNodeId}``（metadata 字面量实锤
       的接口）→ 节点清单；按 icon/avatar/head/face 关键词过滤候选
    ④ 逐个下载候选、按魔数分类（PNG 直用 / UnityFS 需离线解包）；
       命中 1004 编号的裸 PNG 直接落 ``services/avatar_service`` 运行时缓存

【安全】只发 GET；不碰游戏网关写接口；产物只写 ``tmp/``（git 不跟踪）。

用法::

    python tools/probe_avatar_cdn.py                 # 账号库第一个账号登录 + 探测
    python tools/probe_avatar_cdn.py --account 1     # 指定账号
    python tools/probe_avatar_cdn.py --skip-login    # 只握手+探清单（用库里旧编号）
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

import config  # noqa: E402  （依赖仓库根在 sys.path）
from client.account_store import AccountStore  # noqa: E402
from client.game_client import GameClient  # noqa: E402
from services import avatar_service  # noqa: E402

OUT_PATH = ROOT_DIR / "tmp" / "avatar_probe.json"

#: 节点清单里按这些关键词找头像候选（不区分大小写）
KEYWORDS = ("icon", "avatar", "head", "face", "portrait", "hero")

#: 下载候选数量上限（避免一次拖回几十 MB）
DOWNLOAD_LIMIT = 8

UA = {"User-Agent": "Mozilla/5.0 (CherrytaleTool avatar probe)"}

# 游戏基础设施用私有 CA 证书（工具自身会话也走 config.VERIFY_TLS），
# 探测一律沿用同一个开关，避免假性连接失败。
SESSION = requests.Session()
SESSION.verify = config.VERIFY_TLS


def collect_handshake() -> dict[str, Any]:
    """握手拿 CDN 配置（不需要登录态，失败直接抛）。"""
    with GameClient() as client:
        res = client.handshake()
    return {
        "serverIP": res.serverIP,
        "serverPort": res.serverPort,
        "cdnList": list(res.cdnList or []),
        "dnsList": list(res.dnsList or []),
        # settingsNodeId 是 99016 响应的**独立 proto 字段**（不在 jsonInfo 里）
        "settingsNodeId": str(getattr(res, "settingsNodeId", "") or ""),
        "jsonInfo": res.json_settings(),
    }


def run_login(selector: str | None) -> dict[str, Any]:
    """真实登录一次（main.py run login），把 1004 头像编号写进账号库。"""
    cmd = [sys.executable, str(ROOT_DIR / "main.py"), "run", "login"]
    if selector:
        cmd += ["--account", selector]
    proc = subprocess.run(
        cmd,
        cwd=str(ROOT_DIR),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )
    tail = "\n".join((proc.stdout or "").splitlines()[-25:])
    return {"returncode": proc.returncode, "output_tail": tail}


def collect_avatar_ids() -> list[dict[str, Any]]:
    """从账号库收齐各账号各区的头像编号快照。"""
    out: list[dict[str, Any]] = []
    for saved in AccountStore().list_accounts():
        for sid, entry in (saved.sessions or {}).items():
            avatar = entry.get("avatar")
            if avatar:
                out.append(
                    {
                        "account": saved.account,
                        "server_id": sid,
                        "player_name": entry.get("player_name", ""),
                        "avatar": avatar,
                        "has_png": bool(entry.get("avatar_png")),
                    }
                )
    return out


def candidate_urls(manifest_text: str) -> list[str]:
    """从清单文本里挑出头像素材候选（关键词命中即算，保序去重）。"""
    hits: list[str] = []
    for line in manifest_text.splitlines():
        token = line.strip()
        if not token or len(token) > 300:
            continue
        low = token.lower()
        if any(k in low for k in KEYWORDS) and any(
            ext in low for ext in (".png", ".jpg", "bundle", "assets")
        ):
            # 行可能是 "md5  路径" —— 取最长的那段当路径
            path = sorted(token.split(), key=len, reverse=True)[0]
            if path not in hits:
                hits.append(path)
    return hits[:60]


def sniff(data: bytes) -> str:
    """按魔数粗判文件类型。"""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"UnityFS"):
        return "unityfs"
    if data.startswith(b"PK"):
        return "zip"
    return f"unknown(head={data[:16].hex()})"


def probe_downloads(candidates: list[str], cdns: list[str], icon_id: int) -> list[dict[str, Any]]:
    """逐个下载候选：记录类型；裸 PNG 且编号命中 1004 就落运行时缓存。"""
    results: list[dict[str, Any]] = []
    for path in candidates[:DOWNLOAD_LIMIT]:
        for base in cdns:
            url = f"{base.rstrip('/')}/{path.lstrip('/')}"
            entry: dict[str, Any] = {"path": path, "url": url}
            try:
                resp = SESSION.get(url, headers=UA, timeout=20)
                entry["status"] = resp.status_code
                entry["bytes"] = len(resp.content)
                entry["kind"] = sniff(resp.content) if resp.status_code == 200 else "-"
                if entry["kind"] == "png":
                    hit = avatar_service.cache_avatar_png(icon_id, resp.content)
                    entry["cached_as"] = str(hit) if hit else None
                    results.append(entry)
                    return results  # 命中编号头像即收工
            except requests.RequestException as exc:
                entry["error"] = str(exc)[:200]
            results.append(entry)
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--account", default=None, help="账号库选择器（序号/邮箱），默认第一个")
    parser.add_argument("--skip-login", action="store_true", help="跳过真实登录（用库里已有编号）")
    parser.add_argument("--node", default=None, help="手动指定 settingsNodeId（默认取握手下发值）")
    args = parser.parse_args()

    report: dict[str, Any] = {"generated_at": datetime.now().isoformat(timespec="seconds")}

    print("① 握手…")
    hs = collect_handshake()
    report["handshake"] = hs
    print(f"   cdnList={hs['cdnList']}  settingsNodeId={hs['settingsNodeId']!r}")

    if args.skip_login:
        print("② 跳过登录（--skip-login）")
    else:
        print("② 真实登录一次（main.py run login）…")
        report["login"] = run_login(args.account)
        tail = report["login"]["output_tail"]
        interesting = [ln for ln in tail.splitlines() if "cdnUrl" in ln or "头像" in ln]
        for line in interesting:
            print(f"   | {line}")

    report["avatar_ids"] = collect_avatar_ids()
    icon_id = 0
    for item in report["avatar_ids"]:
        icon_id = int(item["avatar"].get("icon") or 0)
        if icon_id:
            break
    print(f"   账号库头像编号：{report['avatar_ids'] or '（无）'}，首个 icon={icon_id}")

    node = args.node or hs["settingsNodeId"]
    api_bases = [f"https://{config.GAME_API_HOST}"] + list(hs["cdnList"])
    print("③ 拉节点清单…")
    manifests: list[dict[str, Any]] = []
    manifest_text = ""
    for base in api_bases:
        for path in (f"/api/getMd5ByNode_V2.jsp?node={node}", f"/getMd5ByNode_V2.jsp?node={node}"):
            url = f"{base.rstrip('/')}{path}"
            entry: dict[str, Any] = {"url": url}
            try:
                resp = SESSION.get(url, headers=UA, timeout=20)
                entry["status"] = resp.status_code
                entry["bytes"] = len(resp.content)
                body = resp.text
                entry["head"] = body[:500]
                if resp.status_code == 200 and len(body) > 40:
                    if not manifest_text:
                        manifest_text = body
            except requests.RequestException as exc:
                entry["error"] = str(exc)[:200]
            manifests.append(entry)
    report["manifest_probes"] = manifests

    if not manifest_text:
        print("   ✗ 所有节点清单 URL 都没拿到内容 —— 结论记入 tmp/avatar_probe.json，"
              "下一步需要抓一次真实客户端的 patch 流量补 URL 形态")
    else:
        candidates = candidate_urls(manifest_text)
        report["candidates"] = candidates
        print(f"   清单拿到（{len(manifest_text)} 字节），头像素材候选 {len(candidates)} 个")
        if candidates and icon_id:
            print("④ 试下载…")
            report["downloads"] = probe_downloads(candidates, api_bases, icon_id)
            for d in report.get("downloads", [])[:DOWNLOAD_LIMIT]:
                print(f"   {d.get('status')} {d.get('kind','-'):9s} {d['path']}")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结论已写入 {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
