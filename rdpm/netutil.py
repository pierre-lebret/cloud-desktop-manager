"""Réseau : IP publique, CIDR, règles de pare-feu RDP et sonde RDP."""

from __future__ import annotations

import ipaddress
import logging
import socket

import requests

from .constants import RDP_PORT
from .models import FirewallRule

log = logging.getLogger(__name__)

# IPv4 uniquement : les serveurs sont créés sans IPv6, l'IP à autoriser est donc l'IPv4.
IP_SERVICES = ("https://api.ipify.org", "https://ipv4.icanhazip.com", "https://ifconfig.me/ip")

# TPKT + X.224 Connection Request avec RDP_NEG_REQ (TLS | CredSSP).
_X224_CR = bytes.fromhex("030000130ee000000000000100080003000000")


def detect_public_ip(timeout: float = 4.0) -> str | None:
    for url in IP_SERVICES:
        try:
            text = requests.get(url, timeout=timeout).text.strip()
            return str(ipaddress.IPv4Address(text))
        except (requests.RequestException, ValueError):
            continue
    log.warning("IP publique introuvable")
    return None


def normalize_cidr(text: str) -> str:
    """'1.2.3.4' → '1.2.3.4/32'. Lève ValueError avec un message en français."""
    text = text.strip()
    if not text:
        raise ValueError("Adresse vide")
    try:
        net = ipaddress.ip_network(text, strict=False)
    except ValueError:
        raise ValueError(f"« {text} » n'est pas une adresse IP ou un réseau CIDR valide") from None
    if net.prefixlen == 0:
        raise ValueError("Autoriser tout Internet (/0) exposerait RDP au monde entier : refusé")
    if net.version == 4 and net.prefixlen < 16:
        raise ValueError("Plage trop large (moins de /16) : précisez l'adresse")
    return str(net)


def ip_allowed(ip: str | None, cidrs: list[str]) -> bool:
    if not ip:
        return False
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for cidr in cidrs:
        try:
            if addr in ipaddress.ip_network(cidr, strict=False):
                return True
        except ValueError:
            continue
    return False


def rdp_sources(rules: tuple[FirewallRule, ...] | list[FirewallRule]) -> list[tuple[str, str]]:
    """Sources autorisées sur le port RDP, dans l'ordre, avec leur description."""
    seen: dict[str, str] = {}
    for rule in rules:
        if rule.is_rdp:
            for cidr in rule.source_ips:
                seen.setdefault(cidr, rule.description or "")
    return list(seen.items())


def with_source_added(rules: list[FirewallRule], cidr: str, description: str) -> list[FirewallRule]:
    """Une règle TCP + une règle UDP par source (l'UDP rend RDP nettement plus fluide)."""
    result = list(rules)
    for proto in ("tcp", "udp"):
        covered = any(r.is_rdp and r.protocol == proto and cidr in r.source_ips for r in result)
        if not covered:
            result.append(FirewallRule("in", proto, RDP_PORT, (cidr,), description[:255] or None))
    return result


def with_source_removed(rules: list[FirewallRule], cidr: str) -> list[FirewallRule]:
    result = []
    for rule in rules:
        if rule.is_rdp and cidr in rule.source_ips:
            remaining = tuple(ip for ip in rule.source_ips if ip != cidr)
            if remaining:
                result.append(FirewallRule(rule.direction, rule.protocol, rule.port, remaining,
                                           rule.description))
        else:
            result.append(rule)
    return result


def with_udp_normalized(rules: list[FirewallRule]) -> list[FirewallRule]:
    result = list(rules)
    for cidr, desc in rdp_sources(rules):
        result = with_source_added(result, cidr, desc)
    return result


def rdp_probe(host: str, port: int = int(RDP_PORT), timeout: float = 3.0) -> bool:
    """Vrai si le service Bureau à distance répond (X.224 Connection Confirm), pas seulement le port."""
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(_X224_CR)
            resp = sock.recv(64)
    except OSError:
        return False
    return len(resp) >= 6 and resp[0] == 3 and resp[5] == 0xD0
