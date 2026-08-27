import sys, os
sys.path.insert(0, r"D:\SmarTAI\powellzacherymt0rxzw_sorce")
os.chdir(r"D:\SmarTAI\powellzacherymt0rxzw_sorce")

from backend.llm.endpoint_policy import (
    _validate_public_addresses,
    resolve_public_endpoint,
)

print("=== 单元校验 _validate_public_addresses ===")
cases = [
    (("114.214.240.204",), "公网 -> 应允许"),
    (("198.18.0.54",), "TUN保留段 -> 应允许(修改目标)"),
    (("114.214.240.204", "198.18.0.54"), "公网+TUN -> 应允许"),
    (("10.0.0.1",), "内网 -> 拒绝"),
    (("127.0.0.1",), "回环 -> 拒绝"),
    (("100.64.166.122",), "CGNAT -> 拒绝"),
    (("169.254.169.254",), "链路本地 -> 拒绝"),
    (("192.0.2.1",), "TEST-NET -> 拒绝"),
]
for addrs, label in cases:
    err = _validate_public_addresses(addrs)
    print(f"  {'允许' if err is None else '拒绝':3s} {addrs}  [{label}]")

print("=== resolve_public_endpoint('https://api.llm.ustc.edu.cn') ===")
try:
    ep = resolve_public_endpoint("https://api.llm.ustc.edu.cn")
    print("  结果: hostname=", ep.hostname, "addresses=", ep.addresses)
except Exception as e:
    print("  抛错:", type(e).__name__, str(e)[:80])
