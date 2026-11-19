"""Country-code -> Chinese name / continent mapping for the node picker.

The upstream vpn-serverlist only carries ISO-ish codes and English names.
The panel shows Chinese labels grouped by continent; this module is the single
source of that mapping. Unknown codes fall back to the upstream English name
and the "其他" continent so a newly added country never breaks the panel.
"""
from __future__ import annotations

# code -> (Chinese name, continent)
COUNTRIES: dict[str, tuple[str, str]] = {
    # 亚洲
    "CN": ("中国", "亚洲"),
    "KR": ("韩国", "亚洲"),
    "JP": ("日本", "亚洲"),
    "SG": ("新加坡", "亚洲"),
    "MY": ("马来西亚", "亚洲"),
    "TH": ("泰国", "亚洲"),
    "PH": ("菲律宾", "亚洲"),
    "IN": ("印度", "亚洲"),
    "HK": ("中国香港", "亚洲"),
    "TW": ("中国台湾", "亚洲"),
    # 欧洲
    "GB": ("英国", "欧洲"),
    "IE": ("爱尔兰", "欧洲"),
    "FR": ("法国", "欧洲"),
    "DE": ("德国", "欧洲"),
    "IT": ("意大利", "欧洲"),
    "ES": ("西班牙", "欧洲"),
    "PT": ("葡萄牙", "欧洲"),
    "NL": ("荷兰", "欧洲"),
    "BE": ("比利时", "欧洲"),
    "AT": ("奥地利", "欧洲"),
    "CH": ("瑞士", "欧洲"),
    "SE": ("瑞典", "欧洲"),
    "NO": ("挪威", "欧洲"),
    "DK": ("丹麦", "欧洲"),
    "FI": ("芬兰", "欧洲"),
    "PL": ("波兰", "欧洲"),
    "CZ": ("捷克", "欧洲"),
    "BG": ("保加利亚", "欧洲"),
    "RO": ("罗马尼亚", "欧洲"),
    "GR": ("希腊", "欧洲"),
    "HU": ("匈牙利", "欧洲"),
    # 北美洲
    "US": ("美国", "北美洲"),
    "CA": ("加拿大", "北美洲"),
    "MX": ("墨西哥", "北美洲"),
    # 南美洲
    "BR": ("巴西", "南美洲"),
    "AR": ("阿根廷", "南美洲"),
    "CL": ("智利", "南美洲"),
    "CO": ("哥伦比亚", "南美洲"),
    "PE": ("秘鲁", "南美洲"),
    # 非洲
    "ZA": ("南非", "非洲"),
    "GH": ("加纳", "非洲"),
    "EG": ("埃及", "非洲"),
    "NG": ("尼日利亚", "非洲"),
    # 大洋洲
    "AU": ("澳大利亚", "大洋洲"),
    "NZ": ("新西兰", "大洋洲"),
    # 特殊哨兵（serverlist 里的虚拟位置，不是国家）
    "REC": ("推荐节点", "特殊"),
}

CONTINENT_ORDER = ["亚洲", "欧洲", "北美洲", "南美洲", "非洲", "大洋洲", "特殊", "其他"]


def label(code: str, english: str | None = None) -> tuple[str, str]:
    """Return (Chinese label, continent) for a country code."""
    code = (code or "").upper()
    hit = COUNTRIES.get(code)
    if hit:
        return hit
    # 未知代码：回退到英文名，绝不返回空标签
    return (english or code or "未知", "其他")


def sort_key(item) -> tuple[int, str]:
    """Sort helper for (code, meta) pairs: continent order, then code."""
    code, meta = item
    continent = (meta.get("continent") if isinstance(meta, dict) else None) \
        or label(code, meta.get("name") if isinstance(meta, dict) else None)[1]
    try:
        ci = CONTINENT_ORDER.index(continent)
    except ValueError:
        ci = len(CONTINENT_ORDER)
    zh = label(code, meta.get("name") if isinstance(meta, dict) else None)[0]
    return (ci, zh)
