"""拼多多商家后台接入边界。

这个文件故意不实现抓取。

商家后台（mms.pinduoduo.com）的扫码登录和推广报表没有稳定、公开、
可供第三方调用的 API。页面接口会变，也带风控。把 Cookie 或请求签名写进
程序、再拿去请求非官方接口，既容易失效，也不该把店铺会话送到别的机器。

本工具只在店主自己的电脑上保存会话。后续如果接入，应当：

- 用本机 WebView2 打开官方登录页，由店主自己扫码
- Cookie 只进本机数据库，退出登录时删除
- 读取店主在后台本来就能看到的计划、创意、商品报表
- 创意报表映射到测图，商品报表映射到测款
- 字段对不上时保留原始 JSON，不要静默算错 ROI

当前调用会抛出 AdapterNotReady，界面会说明原因。
"""

from __future__ import annotations

from datetime import date, datetime

from app.adapters.base import SyncPayload
from app.errors import AdapterNotReady

_MESSAGE = (
    "还没有接入拼多多商家后台。"
    "官方没有稳定的公开推广报表 API，本工具不会伪造扫码登录，"
    "也不会把 Cookie 发到其他服务器。"
    "请先用「模拟店铺」体验全部功能；接入时应只在本机保存你自己扫码得到的会话。"
)


class PddLiveAdapter:
    mode = "live"

    def fetch(self, *, today: date, now: datetime) -> SyncPayload:
        raise AdapterNotReady(_MESSAGE)
