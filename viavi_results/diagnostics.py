"""Record what the network looked like when the app couldn't reach Google.

When a connection to Google Sheets finally fails, capture() tests the network
from this computer and saves the results to one JSON file in the history
folder, so the cause can be worked out afterwards. It compares:

  * Google's API hosts against other Google, StrataSync and unrelated sites
    (is it everything, Google, or just these hosts?)
  * where each connection dies: name lookup, connecting, or the TLS handshake
  * Python's TLS against the operating system's and curl's (is it how Python connects?)
  * the same address under a different server name, and TLS 1.2 against 1.3
    (is something filtering on the name or the handshake?)
  * who issued the certificate (is something inspecting the traffic?)
  * which network adapter carries each host, proxy settings, running VPN and
    security software, and recent updates (what's in the path, and what changed?)

Nothing is sent but connections to those public sites: no credentials, and no
data from the sheet. A failure in here is noted in the file, and never raised.
Drops that a retry recovers from are noted separately in connection_drops.csv.
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import platform
import re
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional
from urllib.parse import urlparse

import certifi
import requests
import urllib3

from . import history
from .config import STRATASYNC_URLS

GOOGLE_API_HOSTS = ["sheets.googleapis.com", "oauth2.googleapis.com"]
OTHER_SNI = "www.google.com"
CONTROL_HOSTS = ["www.google.com", "github.com", "www.microsoft.com"]
HOSTS = GOOGLE_API_HOSTS + [urlparse(u).hostname or "" for u in STRATASYNC_URLS.values()] + CONTROL_HOSTS

_TIMEOUT = 8  # seconds, for each network step
_HOST_BUDGET = 30  # no new attempts on a host after this long
_TOTAL_BUDGET = 55  # the whole capture gives up waiting after this long
_COMMAND_TIMEOUT = 40
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # Windows: no console flashing up

_SECURITY_SOFTWARE = (
    "zscaler|globalprotect|pangp|anyconnect|vpnagent|cisco secure|forticlient|fortissl|netskope|stagent|openvpn|"
    "wireguard|tailscale|warp|umbrella|ivanti|pulse secure|crowdstrike|csfalcon|sentinel|cylance|carbonblack|"
    "symantec|ccsvchst|mcafee|trellix|sophos|kaspersky|ekrn|eset|bitdefender|avast|webroot|msmpeng"
)

# One line when joined: double quotes would need escaping on the command line.
_WINDOWS_SCRIPT = r"""
[Console]::OutputEncoding = [Text.Encoding]::UTF8;
$ErrorActionPreference = 'Stop';
$out = @{};
function Section($name, $body) { try { $out[$name] = @(& $body) } catch { $out[$name] = 'ERROR: ' + $_.Exception.Message } };
$hosts = @(__HOSTS__);
$tools = '__TOOLS__';
Section 'adapters' { Get-NetAdapter | Select-Object Name, InterfaceDescription, @{n='Status';e={[string]$_.Status}}, LinkSpeed };
Section 'mtu' { Get-NetIPInterface -AddressFamily IPv4 | Select-Object InterfaceAlias, NlMtu, InterfaceMetric, @{n='State';e={[string]$_.ConnectionState}} };
Section 'dns_servers' { Get-DnsClientServerAddress -AddressFamily IPv4 | Where-Object { $_.ServerAddresses } | Select-Object InterfaceAlias, ServerAddresses };
Section 'winhttp_proxy' { (netsh winhttp show proxy | Out-String).Trim() };
Section 'processes' { Get-Process | Where-Object { $_.ProcessName -match $tools } | Sort-Object ProcessName -Unique | Select-Object ProcessName, @{n='Version';e={$_.FileVersion}} };
Section 'services' { Get-Service | Where-Object { ($_.Name + ' ' + $_.DisplayName) -match $tools } | Select-Object Name, DisplayName, @{n='Status';e={[string]$_.Status}} };
Section 'recent_updates' { Get-HotFix | Sort-Object InstalledOn -Descending | Select-Object -First 5 HotFixID, @{n='InstalledOn';e={[string]$_.InstalledOn}} };
Section 'recent_installs' { $since = (Get-Date).AddDays(-14).ToString('yyyyMMdd'); Get-ItemProperty 'HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*', 'HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*' -ErrorAction SilentlyContinue | Where-Object { $_.InstallDate -ge $since -and $_.DisplayName } | Select-Object DisplayName, DisplayVersion, InstallDate };
$out['hosts'] = @{};
foreach ($h in $hosts) {
  $r = @{};
  try {
    $ip = ([Net.Dns]::GetHostAddresses($h) | Where-Object { $_.AddressFamily -eq 'InterNetwork' } | Select-Object -First 1).IPAddressToString;
    $r['ip'] = $ip;
    $r['route'] = @(Find-NetRoute -RemoteIPAddress $ip | Select-Object InterfaceAlias, IPAddress, NextHop, DestinationPrefix);
  } catch { $r['route_error'] = $_.Exception.Message };
  try {
    $c = New-Object Net.Sockets.TcpClient;
    if (-not $c.ConnectAsync($h, 443).Wait(8000)) { throw 'TCP connect timed out' };
    $c.ReceiveTimeout = 8000; $c.SendTimeout = 8000;
    $s = New-Object Net.Security.SslStream($c.GetStream(), $false, { $true });
    $s.AuthenticateAsClient($h);
    $r['schannel_ok'] = $true; $r['issuer'] = $s.RemoteCertificate.Issuer; $r['subject'] = $s.RemoteCertificate.Subject; $r['protocol'] = [string]$s.SslProtocol;
    $c.Close();
  } catch { $r['schannel_ok'] = $false; $r['schannel_error'] = $_.Exception.ToString() };
  $out['hosts'][$h] = $r;
};
$out | ConvertTo-Json -Depth 6 -Compress
"""


def _redact(text: Any) -> str:
    """Remove any user name and password from a proxy address."""
    return re.sub(r"//[^/@\s]+@", "//<redacted>@", str(text))


def _guarded(fn: Callable[[], Any]) -> Any:
    try:
        return fn()
    except Exception as exc:
        return {"collection_error": f"{type(exc).__name__}: {exc}"}


def _run(args: List[str], timeout: float = _COMMAND_TIMEOUT) -> Dict[str, Any]:
    proc = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                          stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW)
    return {"exit": proc.returncode, "out": proc.stdout.strip()[:6000], "err": proc.stderr.strip()[:6000]}


# ---- One connection -------------------------------------------------------------


def _describe_certificate(der: bytes, include_pem: bool) -> Dict[str, Any]:
    info: Dict[str, Any] = {"sha256": hashlib.sha256(der).hexdigest()}
    pem = ssl.DER_cert_to_PEM_cert(der)
    if include_pem:
        info["pem"] = pem
    handle, name = tempfile.mkstemp(suffix=".pem")
    try:
        with os.fdopen(handle, "w") as fh:
            fh.write(pem)
        decoded = ssl._ssl._test_decode_cert(name)  # type: ignore[attr-defined]  # parses without trusting it
        for key in ("issuer", "subject"):
            info[key] = ", ".join(f"{k}={v}" for rdn in decoded.get(key, ()) for k, v in rdn)
        info["not_after"] = decoded.get("notAfter")
    except Exception as exc:  # the PEM or fingerprint can still be looked at
        info["decode_error"] = f"{type(exc).__name__}: {exc}"
    finally:
        os.unlink(name)
    return info


def _error_fields(exc: BaseException) -> Dict[str, Any]:
    return {"error_type": type(exc).__name__, "errno": getattr(exc, "errno", None),
            "ssl_reason": getattr(exc, "reason", None), "ssl_library": getattr(exc, "library", None),
            "verify_message": getattr(exc, "verify_message", None), "error": str(exc)}


def _attempt(ip: str, port: int, sni: str, variant: str, context: ssl.SSLContext, *, certificate: bool = False,
             include_pem: bool = False) -> Dict[str, Any]:
    """One TCP connection and TLS handshake, noting how far it got and how long it took."""
    result: Dict[str, Any] = {"variant": variant, "ip": ip, "sni": sni, "ok": False, "failed_at": "tcp"}
    start = time.perf_counter()
    try:
        with socket.create_connection((ip, port), timeout=_TIMEOUT) as sock:
            result["tcp_ms"] = round((time.perf_counter() - start) * 1000)
            result["local_address"] = sock.getsockname()[0]
            result["failed_at"] = "tls"
            with context.wrap_socket(sock, server_hostname=sni) as tls:
                result.update(ok=True, tls_version=tls.version(), cipher=(tls.cipher() or [None])[0])
                del result["failed_at"]
                der = tls.getpeercert(binary_form=True)
                if certificate and der:
                    result["certificate"] = _guarded(lambda: _describe_certificate(der, include_pem))
    except Exception as exc:
        result.update(_error_fields(exc))
    result["elapsed_ms"] = round((time.perf_counter() - start) * 1000)
    return result


def _context(verify: bool, version: Optional[ssl.TLSVersion] = None) -> ssl.SSLContext:
    if verify:
        context = ssl.create_default_context(cafile=certifi.where())  # the list of authorities the app trusts
    else:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    if version:
        context.minimum_version = context.maximum_version = version
    return context


def _requests_probe(host: str, port: int) -> Dict[str, Any]:
    """The same library the app uses, so proxy settings apply as they do for the app."""
    try:
        response = requests.get(f"https://{host}:{port}/", timeout=_TIMEOUT, allow_redirects=False)
        return {"ok": True, "status": response.status_code, "server": response.headers.get("Server")}
    except Exception as exc:
        return {"ok": False, "error_type": type(exc).__name__, "error": str(exc)}


def probe_host(host: str, port: int = 443) -> Dict[str, Any]:
    """Every comparison for one host."""
    result: Dict[str, Any] = {"host": host, "attempts": []}
    start = time.perf_counter()
    try:
        addresses = sorted({a[4][0] for a in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)})
    except Exception as exc:
        result["dns_error"] = _error_fields(exc)
        return result
    result["addresses"] = addresses
    ip = next((a for a in addresses if ":" not in a), addresses[0])

    plans = [(host, "verified, as the app does", _context(True), False)] * 3
    plans.append((host, "certificate, unverified", _context(False), True))
    plans.append((host, "TLS 1.2 only", _context(False, ssl.TLSVersion.TLSv1_2), False))
    plans.append((host, "TLS 1.3 only", _context(False, ssl.TLSVersion.TLSv1_3), False))
    if host in GOOGLE_API_HOSTS:
        plans.append((OTHER_SNI, "same address, different server name", _context(False), False))
    for sni, variant, context, certificate in plans:
        if time.perf_counter() - start > _HOST_BUDGET:
            result["stopped_early"] = f"over {_HOST_BUDGET} seconds"
            break
        result["attempts"].append(_attempt(ip, port, sni, variant, context, certificate=certificate,
                                           include_pem=certificate))
    result["requests_library"] = _requests_probe(host, port)
    return result


# ---- This computer ----------------------------------------------------------------


def _curl(host: str) -> Dict[str, Any]:
    exe = shutil.which("curl")
    if not exe:
        return {"collection_error": "curl not found"}
    result = _run([exe, "-sS", "-v", "--max-time", "12", "-o", os.devnull, f"https://{host}/"], timeout=20)
    return {"exit": result["exit"], "verbose": result["err"][:2500]}


def _windows() -> Dict[str, Any]:
    quoted = ", ".join(f"'{h}'" for h in HOSTS)
    script = " ".join(_WINDOWS_SCRIPT.replace("__HOSTS__", quoted).replace("__TOOLS__", _SECURITY_SOFTWARE).split("\n"))
    result = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script])
    try:
        return json.loads(result["out"].lstrip("﻿"))
    except ValueError:
        return {"collection_error": "PowerShell didn't return JSON", **result}


def _macos() -> Dict[str, Any]:
    routes = {h: _run(["route", "-n", "get", h], timeout=10) for h in HOSTS}
    return {"proxy": _run(["scutil", "--proxy"], timeout=10), "vpn": _run(["scutil", "--nc", "list"], timeout=10),
            "routes": routes}


def _windows_internet_settings() -> Dict[str, Any]:
    if sys.platform != "win32":
        return {}
    import winreg

    found = {}
    path = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
        for name in ("ProxyEnable", "ProxyServer", "AutoConfigURL", "AutoDetect"):
            try:
                found[name] = _redact(winreg.QueryValueEx(key, name)[0])
            except OSError:
                found[name] = None
    return found


def _app_commit() -> Optional[str]:
    from . import updater

    return updater._git("rev-parse", "--short", "HEAD", timeout=10) if updater.supported() else None


def _environment() -> Dict[str, Any]:
    return {
        "user": history._user(),
        "computer": platform.node(),
        "os": platform.platform(),
        "python": sys.version,
        "built_app": bool(getattr(sys, "frozen", False)),
        "app_commit": _guarded(_app_commit),
        "openssl": ssl.OPENSSL_VERSION,
        "requests": requests.__version__,
        "urllib3": urllib3.__version__,
        "certifi": certifi.__version__,
        "ca_file": certifi.where(),
        "certificate_overrides": {k: os.environ.get(k) for k in ("REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "SSL_CERT_FILE",
                                                                 "SSL_CERT_DIR")},
        "proxies_seen_by_python": {k: _redact(v) for k, v in urllib.request.getproxies().items()},
        "windows_internet_settings": _guarded(_windows_internet_settings),
    }


def _error_chain(exc: Optional[BaseException]) -> List[Dict[str, str]]:
    chain = []
    while exc is not None and len(chain) < 8:
        chain.append({"type": f"{type(exc).__module__}.{type(exc).__name__}", "message": str(exc)[:1500]})
        exc = exc.__cause__ or exc.__context__
    return chain


# ---- Saving -----------------------------------------------------------------------


def diagnostics_dir() -> Path:
    return history.history_dir() / "connection_problems"


def collect(error: Optional[BaseException] = None, hosts: Iterable[str] = HOSTS) -> Dict[str, Any]:
    """Run every check at once, stopping after _TOTAL_BUDGET seconds."""
    report: Dict[str, Any] = {
        "captured_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "error": _error_chain(error),
        "environment": _guarded(_environment),
    }
    hosts = list(dict.fromkeys(hosts))
    system = _windows if sys.platform == "win32" else _macos if sys.platform == "darwin" else (lambda: {})
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=2 * len(hosts) + 1)
    probes = {h: pool.submit(_guarded, lambda h=h: probe_host(h)) for h in hosts}
    curls = {h: pool.submit(_guarded, lambda h=h: _curl(h)) for h in hosts}
    system_future = pool.submit(_guarded, system)
    start = time.perf_counter()

    def wait(future: "concurrent.futures.Future[Any]") -> Any:
        try:
            return future.result(timeout=max(0.0, _TOTAL_BUDGET - (time.perf_counter() - start)))
        except concurrent.futures.TimeoutError:
            return {"collection_error": f"not finished after {_TOTAL_BUDGET} seconds"}

    report["hosts"] = {h: {**(wait(probes[h]) or {}), "curl": wait(curls[h])} for h in hosts}
    report["system"] = wait(system_future)
    report["duration_seconds"] = round(time.perf_counter() - start, 1)
    pool.shutdown(wait=False, cancel_futures=True)
    return report


def capture(error: Optional[BaseException] = None) -> Optional[Path]:
    """Save a report on the network right now. Returns its path, or None if it couldn't be saved."""
    try:
        report = collect(error)
        folder = diagnostics_dir()
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"connection-{datetime.now():%Y%m%d-%H%M%S}.json"
        path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        return path
    except Exception:  # never get in the way of reporting the original error
        return None
