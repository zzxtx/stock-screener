# -*- coding: utf-8 -*-
"""临时打包脚本：将 screen.py 打包为单文件 exe，文件名带时间戳，输出到桌面。"""
import datetime
import os

import PyInstaller.__main__ as pm

ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
name = f"股票筛选_{ts}"
home = os.path.expanduser("~")
desktop = os.path.join(home, "Desktop")
if not os.path.isdir(desktop):
    desktop = os.getcwd()
script = os.path.join(os.getcwd(), "screen.py")

pm.run([
    script,
    "--onefile",
    "--console",
    f"--name={name}",
    f"--distpath={desktop}",
    "--workpath=build_tmp",
    "--specpath=build_tmp",
    "--clean",
])
print("BUILD_NAME=" + name)