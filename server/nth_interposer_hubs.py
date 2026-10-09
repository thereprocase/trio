"""Hub trust is operator-owned. DNS checks also guard reconnects against rebinding."""
import ipaddress
from contextlib import contextmanager
import json
from pathlib import Path
import shlex
import socket
import threading
import tomllib
import unicodedata
from urllib.parse import urlsplit

from nth_interposer_wire import WireError, validate_hub, canonical_server

DNS_TIMEOUT = .3
DNS_WORKERS = 4
_dns_slots = threading.BoundedSemaphore(DNS_WORKERS)
_dns_context = threading.local()


class StoreLock:
    """An RLock that also marks its owner's context as unsuitable for DNS."""
    def __init__(self):
        self._lock = threading.RLock()

    def acquire(self, blocking=True, timeout=-1):
        acquired = self._lock.acquire(blocking, timeout)
        if acquired:
            _dns_context.locks = getattr(_dns_context,'locks',0)+1
        return acquired

    def release(self):
        self._lock.release()
        _dns_context.locks -= 1

    def _is_owned(self):
        return self._lock._is_owned()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *args):
        self.release()


@contextmanager
def service_thread():
    previous = getattr(_dns_context,'service',False)
    _dns_context.service = True
    try:
        yield
    finally:
        _dns_context.service = previous


def require_dns_context():
    if getattr(_dns_context,'service',False) or getattr(_dns_context,'locks',0):
        raise WireError('DNS validation requires an unlocked background context', 'hub_not_allowed')


def resolve(host, port):
    """Bound latency and outstanding OS lookups, including ones that never return."""
    require_dns_context()
    slots = _dns_slots
    if not slots.acquire(blocking=False):
        raise WireError('hub resolver capacity unavailable', 'hub_not_allowed')
    done, result = threading.Event(), []
    def lookup():
        try:
            result.append(socket.getaddrinfo(host, port, type=socket.SOCK_STREAM))
        except Exception:
            result.append(None)
        finally:
            slots.release()
            done.set()
    worker = threading.Thread(target=lookup, name='interposer-dns', daemon=True)
    try:
        worker.start()
    except BaseException:
        slots.release()
        raise
    if not done.wait(DNS_TIMEOUT) or not result or not result[0]:
        raise WireError('hub hostname cannot be safely resolved', 'hub_not_allowed')
    return result[0]


def normalized_host(host):
    return ''.join(str(unicodedata.decimal(c)) if c.isdecimal() else c
                   for c in unicodedata.normalize('NFKC', host)).lower().rstrip('.')


def restricted_address(value):
    address = ipaddress.ip_address(value)
    if address.version == 6:
        if any(address in ipaddress.ip_network(prefix) for prefix in ('64:ff9b::/96', '2002::/16')):
            return True
        embedded = address.ipv4_mapped or address.sixtofour
        if address in ipaddress.ip_network('64:ff9b::/96'):
            embedded = ipaddress.IPv4Address(int(address) & 0xffffffff)
        if embedded and restricted_address(str(embedded)):
            return True
    return (address.is_loopback or address.is_link_local or address.is_unspecified
            or str(address) in ('100.100.100.200', 'fd00:ec2::254', '168.63.129.16', '192.0.0.192'))


def restricted_host(url):
    host = normalized_host(urlsplit(url).hostname)
    if host == 'localhost' or host.endswith('.localhost') or host in (
            'metadata.google.internal', 'metadata.goog', 'instance-data.ec2.internal',
            'metadata.azure.internal', 'metadata', 'instance-data'):
        return True
    try:
        return restricted_address(host)
    except ValueError:
        try:
            return restricted_address(socket.inet_ntoa(socket.inet_aton(host)))
        except (OSError, UnicodeError, ValueError):
            return host.isdigit() or host.startswith('0x')


def check_host(url, *, allow_restricted=False):
    """Fail closed on any restricted DNS answer, including mixed/public answers.

    The only exception is an exact URL from the user's trusted local MCP config.
    Resolve on each new connection so approval cannot bypass a subsequent rebind.
    """
    require_dns_context()
    if restricted_host(url) and not allow_restricted:
        raise WireError('loopback, link-local and metadata hub hosts are forbidden', 'hub_not_allowed')
    parts = urlsplit(url)
    host = normalized_host(parts.hostname)
    try:
        answers = resolve(host, parts.port or (443 if parts.scheme == 'https' else 80))
        if not answers:
            raise OSError
        if not allow_restricted and any(restricted_address(a[4][0]) for a in answers):
            raise WireError('loopback, link-local and metadata hub hosts are forbidden', 'hub_not_allowed')
    except WireError:
        raise
    except (OSError, ValueError):
        raise WireError('hub hostname cannot be safely resolved', 'hub_not_allowed') from None


def config_hubs(root=None):
    root = Path(root) if root is not None else Path.home()
    for path, toml in ((root / '.claude.json', False), (root / '.codex' / 'config.toml', True)):
        try:
            data = tomllib.loads(path.read_text()) if toml else json.loads(path.read_text())
            config = data.get('mcp_servers' if toml else 'mcpServers', {})
            if not isinstance(config, dict):
                continue
        except (OSError, ValueError, AttributeError):
            continue
        for server, entry in config.items():
            try:
                if not isinstance(entry, dict):
                    continue
                args = entry.get('args', [])
                if isinstance(args, str):
                    args = shlex.split(args)
                if not isinstance(args, list) or not all(isinstance(v, str) for v in args):
                    continue
                command = [str(entry.get('command', '')), *args]
                if not any(Path(v).name in ('nth_quartet_proxy.py', 'nth_quartet_proxy') for v in command):
                    continue
                url = args[args.index('--url') + 1]
                server = canonical_server(server)
                # A user's own config is the trust authority, including intentional local hubs.
                validate_hub(server, url, allow_restricted=True)
                yield server, url
            except (WireError, ValueError, IndexError):
                continue


def connection_guard(url, *, allow_restricted=False):
    """Pin each HTTP connection to checked DNS answers; avoid a second lookup."""
    def connect(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None):
        host,port = address
        if normalized_host(host)!=normalized_host(urlsplit(url).hostname):
            raise WireError('hub connection origin changed', 'hub_not_allowed')
        check_host(url,allow_restricted=allow_restricted)
        answers = resolve(normalized_host(host),port)
        if not allow_restricted and any(restricted_address(a[4][0]) for a in answers):
            raise WireError('hub DNS answer is restricted', 'hub_not_allowed')
        last = None
        for family,kind,protocol,_,endpoint in answers:
            sock = socket.socket(family,kind,protocol)
            try:
                if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                    sock.settimeout(timeout)
                if source_address:
                    sock.bind(source_address)
                sock.connect(endpoint)
                return sock
            except OSError as exc:
                last = exc
                sock.close()
        raise last or OSError('hub has no usable DNS answers')
    return connect
