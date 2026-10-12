"""Public egress observation; no exchange credentials are sent to the IP service."""

import ipaddress
import os
import urllib.request


def public_ip() -> str:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open("https://api.ipify.org", timeout=5) as response:
        address = ipaddress.ip_address(response.read(65).decode().strip())
    if not address.is_global or address.version != 4:
        raise ValueError("INVALID_EGRESS_IP")
    return str(address)


def configured_ips() -> list[str]:
    """Explicit operator list only; never learn or approve observed addresses."""
    result = []
    for value in os.getenv("LIVE_APPROVED_EGRESS_IPS", "").replace(",", " ").split():
        address = ipaddress.ip_address(value)
        if not address.is_global or address.version != 4:
            raise ValueError("INVALID_EGRESS_ALLOWLIST")
        result.append(str(address))
    return sorted(set(result))
