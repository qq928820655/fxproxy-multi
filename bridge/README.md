# vless/ws bridge（可选部署）

外部客户端经面板单端口使用代理的桥接层。

## 组件

- `xray`（Xray-core 单文件二进制，https://github.com/XTLS/Xray-core/releases 下载 linux-64 版）
- `config.json`：vless+ws 入站（127.0.0.1:8081, path /vless）→ socks 出站（127.0.0.1:1080 = fxproxy）
- `uuid.txt`：VLESS uuid（`python3 -c "import uuid;print(uuid.uuid4())" > uuid.txt`）

## 首次生成

```bash
# 下载 xray 二进制放本目录，生成 uuid
python3 -c "import uuid;print(uuid.uuid4())" > uuid.txt
# 把 uuid.txt 里的值填进 config.json 的 clients[0].id（参照 config.example.json）
./xray run -c config.json &
```

面板自动检测 `bridge/uuid.txt`：存在且面板经公网域名访问时，
`/clash.yaml` 追加 `fxproxy-vless-ws` 节点、`/sub` 提供 vless:// 订阅、
`/vless` 做 WS 反代。凭据文件（uuid/config/xray 二进制/geo 库）不进 git。
