# -*- coding: utf-8 -*-
"""
在本机 git push 会卡住时用的推送脚本。

背景：这台机器上 `git push` / `git credential fill` 会走进一个交互式凭据选择器
（system 级 credential.helper = helper-selector），终端里没有交互界面时会一直挂着。
本脚本从 GCM 直接取 token，用 `-c credential.helper=` 清空助手列表后再推，
token 只在内存里，不落盘、不打印。

用法：
    python tools/push_with_token.py                  # 推当前分支
    python tools/push_with_token.py v3-limitup-gap   # 推指定分支
"""

import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import upload_release as U                      # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    if len(sys.argv) > 1:
        branch = sys.argv[1]
    else:
        branch = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                                capture_output=True, text=True,
                                encoding="utf-8", cwd=ROOT).stdout.strip()

    tok = U.get_token()
    if not tok:
        print("拿不到 GitHub 凭据")
        return 1

    url = "https://zzxtx:%s@github.com/%s/%s.git" % (tok, U.OWNER, U.REPO)
    cmd = ["git", "-c", "credential.helper=", "push", url,
           "refs/heads/%s" % branch]

    env = dict(os.environ)
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        env.pop(k, None)
    env["GIT_TERMINAL_PROMPT"] = "0"

    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           timeout=150, env=env, cwd=ROOT)
    except subprocess.TimeoutExpired:
        print("超时：150 秒未完成")
        return 2

    out = (r.stdout + r.stderr).replace(tok, "***")
    print(out.strip())
    return 0 if r.returncode == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
