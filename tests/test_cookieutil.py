"""Unit tests for cookieutil (no network / no third-party deps)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fxproxy.cookieutil import parse

T1 = "a" * 64
T2 = "b" * 64
T3 = "c" * 64
R1 = "d" * 64
T_UPPER = "A" * 64


def check(cond, label):
    if not cond:
        raise AssertionError(label)
    print(f"  ok  {label}")


def test_extension_json():
    payload = {
        "version": 1,
        "source": "fxproxy-chrome-extension",
        "accounts": [
            {"uid": "u1", "email": "a@example.com", "session_token": T1},
            {"uid": "u2", "email": "b@example.com", "session_token": T2},
        ],
        "session_tokens": [T1, T2, T3],
    }
    r = parse(__import__("json").dumps(payload))
    check(set(r["session_tokens"]) == {T1, T2, T3}, "extension JSON: 3 deduped tokens")
    check(len(r["accounts"]) == 2, "extension JSON: 2 accounts")
    check(r["accounts"][0]["email"] == "a@example.com", "extension JSON: email preserved")
    check(not r["refresh_tokens"], "extension JSON: no refresh tokens")


def test_cookie_header():
    r = parse(f"sessionToken={T1}; foo=bar; refresh_token={R1}")
    check(T1 in r["session_tokens"], "cookie header: sessionToken extracted")
    check(R1 in r["refresh_tokens"], "cookie header: refresh_token classified")
    check("foo" not in str(r["detected"]), "cookie header: junk ignored")


def test_netscape_cookie_file():
    text = (
        "# Netscape HTTP Cookie File\n"
        f"#HttpOnly_accounts.firefox.com\tTRUE\t/\tTRUE\t0\tsessionToken\t{T1}\n"
        f"accounts.firefox.com\tTRUE\t/\tTRUE\t0\tother\txyz\n"
    )
    r = parse(text)
    check(T1 in r["session_tokens"], "netscape cookie: sessionToken extracted")


def test_bare_tokens():
    r = parse(f"{T_UPPER}\n{T2}, {T3}")
    check(r["session_tokens"] == [T1, T2, T3], "bare tokens: split + lowercase + order")


def test_bare_as_refresh():
    r = parse(T1, bare_as="refresh_token")
    check(r["refresh_tokens"] == [T1] and not r["session_tokens"], "bare_as refresh_token")


def test_json_array():
    r = parse(f'["{T1}", "{T2}", "{T1}"]')
    check(r["session_tokens"] == [T1, T2], "JSON array: deduped")


def test_fxs_prefixed():
    # fxs_ followed by base64url of 32 zero bytes = 64 zeros hex
    import base64
    b64 = base64.urlsafe_b64encode(b"\x00" * 32).decode().rstrip("=")
    r = parse("fxs_" + b64)
    check(r["session_tokens"] == ["0" * 64], "fxs_ prefixed: decoded to hex")


def test_empty():
    try:
        parse("   ")
    except ValueError:
        check(True, "empty input: raises ValueError")
    else:
        check(False, "empty input: raises ValueError")


def test_all_plugin_formats():
    """Every export format from chrome-extension/formats.js must be importable."""
    samples = {
        "json": __import__("json").dumps({
            "version": 1, "source": "fxproxy-chrome-extension",
            "accounts": [{"uid": "u1", "email": "a@b.c", "session_token": T1}],
            "session_tokens": [T1, T2],
        }),
        "lines": f"{T1}\n{T2}\n",
        "cookie": f"sessionToken={T1}; sessionToken={T2}\n",
        "netscape": (
            "# Netscape HTTP Cookie File\n"
            f"accounts.firefox.com\tTRUE\t/\tTRUE\t4102444800\tsessionToken\t{T1}\n"
            f"accounts.firefox.com\tTRUE\t/\tTRUE\t4102444800\tsessionToken\t{T2}\n"
        ),
        "csv": f"{T1}, {T2}\n",
        "accounts": f"a@b.c {T1}\nd@e.f {T2}\n",
    }
    for kind, text in samples.items():
        r = parse(text)
        check(set(r["session_tokens"]) == {T1, T2}, f"plugin format '{kind}': 2 tokens parsed")


if __name__ == "__main__":
    for fn in [
        test_extension_json, test_cookie_header, test_netscape_cookie_file,
        test_bare_tokens, test_bare_as_refresh, test_json_array,
        test_fxs_prefixed, test_empty, test_all_plugin_formats,
    ]:
        print(f"[{fn.__name__}]")
        fn()
    print("\nALL COOKIEUTIL TESTS PASSED")
