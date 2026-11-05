# -*- coding: utf-8 -*-
"""将 screen.py 打包为单文件 exe，文件名带时间戳，输出到桌面（无桌面则输出到 release 目录）。"""
import datetime
import os

import PyInstaller.__main__ as pm

ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
name = "涨停缺口选股_%s" % ts
home = os.path.expanduser("~")
desktop = os.path.join(home, "Desktop")
if not os.path.isdir(desktop):
    desktop = os.path.join(os.path.dirname(os.getcwd()), "release")
    os.makedirs(desktop, exist_ok=True)
script = os.path.join(os.getcwd(), "screen.py")

pm.run([
    script,
    "--onefile",
    "--console",
    "--name=%s" % name,
    "--distpath=%s" % desktop,
    "--workpath=build_tmp",
    "--specpath=build_tmp",
    "--clean",
])
print("BUILD_NAME=" + name)
