# -*- coding: utf-8 -*-
"""
把 release/ 目录下的 apk / exe 上传到 GitHub Releases，供别人直接下载。

用法：
    python tools/upload_release.py v1.2
    python tools/upload_release.py v1.2 --note "本次更新说明"

依赖：只用标准库。鉴权走 git 已保存的凭据（git credential fill），
      不读环境变量、不写配置文件、不打印 token 本身。
要求：release/ 下已有构建产物；本机 git 已能免密 push 这个仓库。
"""

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

OWNER, REPO = "zzxtx", "stock-screener"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RELEASE_DIR = os.path.join(ROOT, "release")

# 本地文件名 -> 发布文件名（发布名用 ASCII + 版本号，避免下载链接里全是百分号编码）
ASSETS = [
    ("涨停缺口选股.apk", "limitup-gap-%s.apk"),
    ("涨停缺口选股.exe", "limitup-gap-%s.exe"),
]

DEFAULT_BODY = """沪深主板「涨停 + 次日跳空缺口 + 筹码收敛」四条件选股器。

**信号条件**（四条同时满足才算命中）
- 股价 3 元 ~ 25 元
- 最近涨停距今 ≥ 2 个交易日，且非连板
- 涨停次日跳空，且至今未回补
- 当日筹码集中度小于上一日

**下载**
- `limitup-gap-%s.apk` —— Android 版，包名 `com.example.limitupgap`，可与其他版本共存安装
- `limitup-gap-%s.exe` —— Windows 桌面版，双击运行，输入日期即可

**说明**
- Android 版首次安装需允许「安装未知来源应用」
- 输出只包含四条条件全部满足的个股
- 数据源为新浪财经公开接口，本项目只做数据筛选，不构成任何投资建议
"""


def get_token():
    """从 git 凭据助手取 token。只在内存里用，不落盘、不打印。"""
    try:
        out = subprocess.run(["git", "credential", "fill"],
                             input="protocol=https\nhost=github.com\n\n",
                             capture_output=True, text=True, encoding="utf-8")
    except FileNotFoundError:
        return ""
    for line in out.stdout.splitlines():
        if line.startswith("password="):
            return line[len("password="):].strip()
    return ""


def _proxy():
    """跟随 git 里配置的 http.proxy，国内网络通常需要。"""
    try:
        out = subprocess.run(["git", "config", "--get", "http.proxy"],
                             capture_output=True, text=True, encoding="utf-8")
        p = out.stdout.strip()
        return {"http": p, "https": p} if p else {}
    except Exception:
        return {}


def api(method, url, token, data=None, ctype="application/json", file_path=None):
    headers = {
        "Authorization": "token %s" % token,
        "Accept": "application/vnd.github+json",
        "User-Agent": "release-uploader",
        "Content-Type": ctype,
    }
    body = None
    if file_path:
        body = open(file_path, "rb").read()
        headers["Content-Length"] = str(len(body))
    elif data is not None:
        body = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler(_proxy())) if _proxy() else urllib.request.build_opener()

    last = None
    for attempt in range(4):          # 代理抖动比较常见，多试几次
        try:
            with opener.open(req, timeout=180) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")
        except Exception as e:        # noqa: BLE001
            last = e
            time.sleep(2 + attempt * 2)
    raise last


def main():
    ap = argparse.ArgumentParser(description="上传安装包到 GitHub Releases")
    ap.add_argument("tag", help="版本号，例如 v1.2")
    ap.add_argument("--target", default="", help="从哪个分支/提交打 tag，默认当前分支")
    ap.add_argument("--note", default="", help="追加到发布说明开头的自定义说明")
    args = ap.parse_args()

    ver = args.tag.lstrip("v")
    token = get_token()
    if not token:
        print("拿不到 GitHub 凭据。先手动 push 一次让 git 记住账号。")
        return 1

    target = args.target
    if not target:
        target = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                                capture_output=True, text=True,
                                encoding="utf-8", cwd=ROOT).stdout.strip()

    body = (args.note + "\n\n" if args.note else "") + DEFAULT_BODY % (ver, ver)

    code, txt = api("GET", "https://api.github.com/repos/%s/%s/releases/tags/%s"
                    % (OWNER, REPO, args.tag), token)
    if code == 200:
        rel = json.loads(txt)
        print("release %s 已存在，复用（id=%d）" % (args.tag, rel["id"]))
    else:
        code, txt = api("POST", "https://api.github.com/repos/%s/%s/releases"
                        % (OWNER, REPO), token,
                        data={"tag_name": args.tag, "target_commitish": target,
                              "name": "涨停缺口选股 %s" % args.tag,
                              "body": body, "draft": False, "prerelease": False})
        if code not in (200, 201):
            print("创建 release 失败 %d：%s" % (code, txt[:400]))
            return 1
        rel = json.loads(txt)
        print("已创建 release %s（target=%s, id=%d）" % (args.tag, target, rel["id"]))

    have = {a["name"] for a in rel.get("assets", [])}
    for local, pattern in ASSETS:
        path = os.path.join(RELEASE_DIR, local)
        if not os.path.exists(path):
            print("跳过（文件不存在）：%s" % path)
            continue
        name = pattern % ver
        if name in have:
            print("已存在，跳过：%s" % name)
            continue
        url = ("https://uploads.github.com/repos/%s/%s/releases/%d/assets?name=%s"
               % (OWNER, REPO, rel["id"], urllib.parse.quote(name)))
        code, txt = api("POST", url, token, ctype="application/octet-stream",
                        file_path=path)
        if code in (200, 201):
            print("上传成功：%s（%.2f MB）"
                  % (name, os.path.getsize(path) / 1048576))
        else:
            print("上传失败 %s %d：%s" % (name, code, txt[:300]))

    code, txt = api("GET", "https://api.github.com/repos/%s/%s/releases/tags/%s"
                    % (OWNER, REPO, args.tag), token)
    if code == 200:
        rel = json.loads(txt)
        print("\n发布页：%s" % rel["html_url"])
        for a in rel.get("assets", []):
            print("  %s（%.2f MB）" % (a["name"], a["size"] / 1048576))
    return 0


if __name__ == "__main__":
    sys.exit(main())
