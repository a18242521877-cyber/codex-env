"""登录二维码。内容只是本机演示票据，不是拼多多登录凭证。"""

from __future__ import annotations

import html


def make_qr_svg(payload: str) -> tuple[str, bool]:
    """返回 (svg, scannable)。未安装 segno 时给出明确的占位图，不伪装成可扫描码。"""
    try:
        import segno
    except ImportError:
        return _fallback_svg(payload), False
    qr = segno.make(payload, error="m")
    try:
        svg = qr.svg_inline(scale=5, dark="#1c2430", light="#ffffff", svgclass="qr-svg")
    except TypeError:
        svg = qr.svg_inline(scale=5)
    if isinstance(svg, bytes):
        svg = svg.decode("utf-8")
    return svg, True


def _fallback_svg(payload: str) -> str:
    safe = html.escape(payload if len(payload) <= 42 else payload[:42] + "…")
    return f"""<svg xmlns="http://www.w3.org/2000/svg" class="qr-svg" viewBox="0 0 240 240" role="img">
      <rect width="240" height="240" rx="12" fill="#f4efe8"/>
      <rect x="18" y="18" width="204" height="204" rx="8" fill="#fff" stroke="#1c2430" stroke-width="2"/>
      <text x="120" y="108" text-anchor="middle" font-size="15" fill="#1c2430">未安装 segno</text>
      <text x="120" y="132" text-anchor="middle" font-size="12" fill="#6f675e">二维码不可扫描</text>
      <text x="120" y="168" text-anchor="middle" font-size="8" fill="#8a8176">{safe}</text>
    </svg>"""
