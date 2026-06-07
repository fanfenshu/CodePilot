"""
Cloudflare DNS 自动配置模块

供 OpenClaw 部署流程调用，自动添加/更新/查询 DNS 记录。
凭证从服务器环境变量或配置文件读取。

使用方式：
    from dns_manager import CloudflareDNS
    dns = CloudflareDNS.from_config()
    dns.ensure_record("sales.vickclaw.com", "47.107.157.5")

配置文件路径：/home/deploy/.cloudflare/config.json
格式：
    {
        "api_token": "xxx",
        "zones": {
            "vickclaw.com": "zone_id_1",
            "vickclaw.ai": "zone_id_2",
            "flyranking.com": "zone_id_3"
        }
    }
"""

import json
import os
import urllib.request
import urllib.error


CONFIG_PATH = "/home/deploy/.cloudflare/config.json"


class CloudflareDNS:
    """Cloudflare DNS 自动配置"""

    API_BASE = "https://api.cloudflare.com/client/v4"

    def __init__(self, api_token, zones):
        """
        Args:
            api_token: Cloudflare API Token (scoped to DNS edit)
            zones: dict of {domain: zone_id}, e.g. {"vickclaw.com": "abc123"}
        """
        self.api_token = api_token
        self.zones = zones

    @classmethod
    def from_config(cls, config_path=None):
        """从配置文件加载凭证"""
        path = config_path or CONFIG_PATH
        if not os.path.exists(path):
            raise FileNotFoundError(
                f"Cloudflare 配置文件不存在: {path}\n"
                f"请先执行一次性配置，创建该文件。"
            )
        with open(path, 'r') as f:
            config = json.load(f)
        return cls(
            api_token=config["api_token"],
            zones=config["zones"]
        )

    def _get_zone_id(self, fqdn):
        """从完整域名推断所属 zone 并返回 zone_id"""
        # 尝试匹配最长的域名后缀
        parts = fqdn.split('.')
        for i in range(len(parts) - 1):
            candidate = '.'.join(parts[i:])
            if candidate in self.zones:
                return self.zones[candidate], candidate
        raise ValueError(
            f"域名 {fqdn} 不属于已配置的任何 zone: {list(self.zones.keys())}"
        )

    def _request(self, method, path, data=None):
        """发送 Cloudflare API 请求"""
        url = f"{self.API_BASE}{path}"
        body = json.dumps(data).encode() if data else None
        req = urllib.request.Request(url, data=body, method=method)
        req.add_header("Authorization", f"Bearer {self.api_token}")
        req.add_header("Content-Type", "application/json")
        try:
            resp = urllib.request.urlopen(req, timeout=30)
            return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            error_body = e.read().decode()
            raise RuntimeError(
                f"Cloudflare API 错误 ({e.code}): {error_body}"
            )

    def list_records(self, domain=None, record_type=None):
        """列出 DNS 记录"""
        if domain:
            zone_id, _ = self._get_zone_id(domain)
            params = f"?name={domain}"
            if record_type:
                params += f"&type={record_type}"
        else:
            # 列出所有 zone 的记录
            results = []
            for zone_domain, zone_id in self.zones.items():
                resp = self._request("GET",
                    f"/zones/{zone_id}/dns_records?per_page=100")
                results.extend(resp.get("result", []))
            return results

        resp = self._request("GET",
            f"/zones/{zone_id}/dns_records{params}")
        return resp.get("result", [])

    def get_record(self, fqdn, record_type="A"):
        """查询单条 DNS 记录"""
        zone_id, _ = self._get_zone_id(fqdn)
        resp = self._request("GET",
            f"/zones/{zone_id}/dns_records?name={fqdn}&type={record_type}")
        records = resp.get("result", [])
        return records[0] if records else None

    def add_record(self, fqdn, content, record_type="A", proxied=True, ttl=1):
        """添加 DNS 记录

        Args:
            fqdn: 完整域名，如 "sales.vickclaw.com"
            content: 记录值（A 记录为 IP，CNAME 为目标域名，TXT 为文本）
            record_type: A / AAAA / CNAME / TXT / MX 等
            proxied: 是否开启 Cloudflare CDN 代理（仅 A/AAAA/CNAME 有效）
            ttl: TTL 秒数，1 = Auto
        """
        zone_id, _ = self._get_zone_id(fqdn)
        data = {
            "type": record_type,
            "name": fqdn,
            "content": content,
            "ttl": ttl,
        }
        # proxied 仅对 A/AAAA/CNAME 有效
        if record_type in ("A", "AAAA", "CNAME"):
            data["proxied"] = proxied
        return self._request("POST",
            f"/zones/{zone_id}/dns_records", data)

    def update_record(self, fqdn, content, record_type="A", proxied=True, ttl=1):
        """更新已有 DNS 记录"""
        zone_id, _ = self._get_zone_id(fqdn)
        record = self.get_record(fqdn, record_type)
        if not record:
            return self.add_record(fqdn, content, record_type, proxied, ttl)
        data = {
            "type": record_type,
            "name": fqdn,
            "content": content,
            "ttl": ttl,
        }
        if record_type in ("A", "AAAA", "CNAME"):
            data["proxied"] = proxied
        return self._request("PUT",
            f"/zones/{zone_id}/dns_records/{record['id']}", data)

    def delete_record(self, fqdn, record_type="A"):
        """删除 DNS 记录"""
        zone_id, _ = self._get_zone_id(fqdn)
        record = self.get_record(fqdn, record_type)
        if not record:
            return {"status": "not_found", "message": f"{fqdn} 不存在"}
        return self._request("DELETE",
            f"/zones/{zone_id}/dns_records/{record['id']}")

    def ensure_record(self, fqdn, content, record_type="A", proxied=True):
        """确保 DNS 记录存在且正确（幂等操作，部署流程首选）

        Returns:
            dict with keys: status ("unchanged" | "updated" | "created"), record
        """
        existing = self.get_record(fqdn, record_type)

        if existing:
            # 检查是否需要更新
            content_match = existing["content"] == content
            proxied_match = (record_type not in ("A", "AAAA", "CNAME")
                          or existing.get("proxied") == proxied)
            if content_match and proxied_match:
                return {"status": "unchanged", "record": existing}
            # 需要更新
            result = self._request("PUT",
                f"/zones/{self.zones[self._get_zone_id(fqdn)[1]]}"
                f"/dns_records/{existing['id']}", {
                    "type": record_type,
                    "name": fqdn,
                    "content": content,
                    "ttl": 1,
                    **({"proxied": proxied}
                       if record_type in ("A", "AAAA", "CNAME") else {})
                })
            return {"status": "updated", "result": result}

        # 不存在，创建
        result = self.add_record(fqdn, content, record_type, proxied)
        return {"status": "created", "result": result}

    def verify_propagation(self, fqdn, expected_ip=None):
        """验证 DNS 是否已生效（通过 Cloudflare DNS 查询）

        注意：Cloudflare 代理模式下解析到的是 Cloudflare IP，不是源站 IP。
        此方法仅验证记录是否存在于 Cloudflare。
        """
        record = self.get_record(fqdn)
        if not record:
            return {"propagated": False, "reason": "记录不存在"}
        if expected_ip and not record.get("proxied") and record["content"] != expected_ip:
            return {"propagated": False,
                    "reason": f"IP 不匹配: {record['content']} != {expected_ip}"}
        return {"propagated": True, "record": record}


# === CLI 入口：支持从命令行直接调用 ===
if __name__ == "__main__":
    import sys

    USAGE = """用法:
    python3 dns_manager.py ensure <fqdn> <ip>       # 确保记录存在
    python3 dns_manager.py add <fqdn> <ip>           # 添加记录
    python3 dns_manager.py get <fqdn>                # 查询记录
    python3 dns_manager.py delete <fqdn>             # 删除记录
    python3 dns_manager.py list [domain]             # 列出记录
    python3 dns_manager.py verify <fqdn> [ip]        # 验证记录
    python3 dns_manager.py test                      # 测试连接
"""

    if len(sys.argv) < 2:
        print(USAGE)
        sys.exit(1)

    cmd = sys.argv[1]

    try:
        dns = CloudflareDNS.from_config()
    except FileNotFoundError as e:
        print(f"错误: {e}")
        sys.exit(1)

    if cmd == "test":
        # 测试 API 连接
        print("测试 Cloudflare API 连接...")
        for domain, zone_id in dns.zones.items():
            try:
                resp = dns._request("GET", f"/zones/{zone_id}")
                zone_name = resp["result"]["name"]
                print(f"  {domain} (zone: {zone_id[:8]}...): 连接正常 ✓")
            except Exception as e:
                print(f"  {domain}: 连接失败 ✗ - {e}")

    elif cmd == "ensure" and len(sys.argv) >= 4:
        fqdn, ip = sys.argv[2], sys.argv[3]
        result = dns.ensure_record(fqdn, ip)
        print(json.dumps(result, indent=2, default=str))

    elif cmd == "add" and len(sys.argv) >= 4:
        fqdn, ip = sys.argv[2], sys.argv[3]
        result = dns.add_record(fqdn, ip)
        print(json.dumps(result, indent=2, default=str))

    elif cmd == "get" and len(sys.argv) >= 3:
        fqdn = sys.argv[2]
        record = dns.get_record(fqdn)
        if record:
            print(json.dumps(record, indent=2))
        else:
            print(f"{fqdn}: 记录不存在")

    elif cmd == "delete" and len(sys.argv) >= 3:
        fqdn = sys.argv[2]
        result = dns.delete_record(fqdn)
        print(json.dumps(result, indent=2, default=str))

    elif cmd == "list":
        domain = sys.argv[2] if len(sys.argv) >= 3 else None
        records = dns.list_records(domain)
        for r in records:
            proxy_icon = "☁" if r.get("proxied") else "→"
            print(f"  {r['type']:6} {r['name']:40} {proxy_icon} {r['content']}")

    elif cmd == "verify" and len(sys.argv) >= 3:
        fqdn = sys.argv[2]
        expected = sys.argv[3] if len(sys.argv) >= 4 else None
        result = dns.verify_propagation(fqdn, expected)
        print(json.dumps(result, indent=2, default=str))

    else:
        print(USAGE)
        sys.exit(1)
