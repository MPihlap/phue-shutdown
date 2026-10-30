import re
import socket
import subprocess
import sys

HOSTNAME = "001788aedf4d"


def resolve_dns(hostname):
    # Try the plain name plus common local suffixes (mDNS, router DNS)
    for name in (hostname, f"{hostname}.local", f"{hostname}.lan", f"{hostname}.home"):
        try:
            return socket.gethostbyname(name)
        except socket.gaierror:
            continue
    return None


def resolve_arp(hostname):
    # Hue bridge hostnames are the MAC address, so look it up in the ARP table
    if not re.fullmatch(r"[0-9a-fA-F]{12}", hostname):
        return None
    mac = hostname.lower()
    output = subprocess.run(["arp", "-a"], capture_output=True, text=True).stdout
    for line in output.splitlines():
        ip = re.search(r"\d+\.\d+\.\d+\.\d+", line)
        found = re.search(r"([0-9a-fA-F]{1,2}[:-]){5}[0-9a-fA-F]{1,2}", line)
        if ip and found:
            normalized = "".join(part.zfill(2) for part in re.split(r"[:-]", found.group(0))).lower()
            if normalized == mac:
                return ip.group(0)
    return None


def find_ip(hostname):
    return resolve_dns(hostname) or resolve_arp(hostname)


if __name__ == "__main__":
    hostname = sys.argv[1] if len(sys.argv) > 1 else HOSTNAME
    ip = find_ip(hostname)
    if ip:
        print(ip)
    else:
        print(f"Could not find IP for {hostname}", file=sys.stderr)
        sys.exit(1)
