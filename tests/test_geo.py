"""国家中文名/洲域映射测试。运行: python tests/test_geo.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fxproxy import geo

PASS = 0


def check(cond, msg):
    global PASS
    assert cond, msg
    PASS += 1
    print(f"  ok  {msg}")


def main():
    print("[geo] 映射")
    check(geo.label("US") == ("美国", "北美洲"), "US -> 美国/北美洲")
    check(geo.label("GB") == ("英国", "欧洲"), "GB -> 英国/欧洲")
    check(geo.label("JP") == ("日本", "亚洲"), "JP -> 日本/亚洲")
    check(geo.label("ZA") == ("南非", "非洲"), "ZA -> 南非/非洲")
    check(geo.label("XX", "Xland") == ("Xland", "其他"), "未知代码回退英文名")

    print("[geo] 排序")
    items = [("US", {"name": "United States"}), ("JP", {"name": "Japan"}),
             ("GB", {"name": "United Kingdom"}), ("ZZ", {"name": "Zed"})]
    ordered = [k for k, _ in sorted(items, key=geo.sort_key)]
    check(ordered.index("JP") < ordered.index("GB") < ordered.index("US"),
          "洲域顺序: 亚洲 < 欧洲 < 北美洲")
    check(ordered[-1] == "ZZ", "未知代码排最后")
    print(f"\nALL GEO TESTS PASSED ({PASS} checks)")


if __name__ == "__main__":
    main()
