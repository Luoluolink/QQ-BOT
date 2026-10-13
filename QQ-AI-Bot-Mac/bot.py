"""NoneBot2 入口文件。

启动：nb run  或  python bot.py
"""

import os

# 剪断与「启动者会话」的代理关联：机器人从别的进程/会话里被拉起时，
# 会继承对方环境里的 HTTP_PROXY 等变量；那些代理指向的是启动会话自己的
# 本地端口，会话一换端口就成死代理，模型 API 全部报 Connection error。
# 机器人需要的只是直连外网（实测直连可达），所以启动时把代理变量全部清掉。
for _k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
           "http_proxy", "https_proxy", "all_proxy"):
    os.environ.pop(_k, None)
os.environ["NO_PROXY"] = "*"

import nonebot
from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter

nonebot.init()

driver = nonebot.get_driver()
driver.register_adapter(OneBotV11Adapter)

nonebot.load_builtin_plugins("echo")
nonebot.load_plugins("plugins")

if __name__ == "__main__":
    nonebot.run()
