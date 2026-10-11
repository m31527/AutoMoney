"""Public egress observation; no exchange credentials are sent to the IP service."""

import ipaddress
import urllib.request


def public_ip() -> str:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open("https://api.ipify.org", timeout=5) as response:
        address = ipaddress.ip_address(response.read(65).decode().strip())
    if not address.is_global or address.version != 4:
        raise ValueError("INVALID_EGRESS_IP")
    return str(address)
