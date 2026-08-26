"""测试夹具构造。

刻意使用**合成**的频道内容与假链接：真实频道内容会随时间变化，
用它做断言的测试今天绿明天红；假数据则让测试只验证解析逻辑本身。
"""

from __future__ import annotations

MESSAGE_TEMPLATE = """
<div class="tgme_widget_message_wrap js-widget_message_wrap">
  <div class="tgme_widget_message js-widget_message" data-post="{channel}/{msg_id}">
    <div class="tgme_widget_message_bubble">
      {reply}
      <div class="tgme_widget_message_text js-message_text" dir="auto">{text}</div>
      <div class="tgme_widget_message_footer">
        <div class="tgme_widget_message_info">
          <a class="tgme_widget_message_date" href="https://t.me/{channel}/{msg_id}">
            <time datetime="{published}" class="time">10:23</time>
          </a>
        </div>
      </div>
    </div>
  </div>
</div>
"""

PAGE_TEMPLATE = """<!DOCTYPE html><html><body>
<div class="tgme_channel_info_header_title"><span>{title}</span></div>
<section class="tgme_channel_history js-message_history">
{messages}
</section>
</body></html>"""


def build_message(
    channel: str,
    msg_id: int,
    text: str,
    published: str = "2026-08-25T07:23:07+00:00",
    reply: str = "",
) -> str:
    return MESSAGE_TEMPLATE.format(
        channel=channel, msg_id=msg_id, text=text, published=published, reply=reply
    )


def build_page(channel: str, messages: list[str], title: str = "测试频道") -> str:
    return PAGE_TEMPLATE.format(title=title, messages="\n".join(messages))


def simple_page(channel: str, ids: list[int]) -> str:
    """一页常规消息，每条含一个假的网盘链接。"""
    return build_page(
        channel,
        [
            build_message(
                channel,
                i,
                f"名称：<b>测试剧集{i}</b><br/>"
                f'链接：<a href="https://pan.quark.cn/s/fake{i:06d}" '
                f'target="_blank">https://pan.quark.cn/s/fake{i:06d}</a>',
            )
            for i in ids
        ],
    )
