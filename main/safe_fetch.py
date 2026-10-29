"""Fetching user-supplied URLs (recipe pages and their images) without letting
them reach the server's own network.

The URL importer and image scraper fetch whatever address a user gives them.
Left alone, that lets anyone with recipe-write access make the server request
internal addresses: localhost, the LAN, or a cloud provider's metadata service.
`get()` refuses any URL whose host resolves to a non-public address, and
follows redirects itself so each hop is checked too.

The check resolves the host and requests then resolves it again, so a DNS
server that answers differently the second time could still slip past. This
stops casual misuse; it isn't a full network sandbox.
"""
import ipaddress
import socket
from urllib.parse import urljoin, urlsplit

import requests

MAX_REDIRECTS = 5


class UnsafeURL(requests.RequestException):
    """The URL points somewhere the server mustn't fetch from. A
    RequestException, so callers' existing network-error handling covers it."""


def _check(url):
    parts = urlsplit(url)
    if parts.scheme not in ('http', 'https') or not parts.hostname:
        raise UnsafeURL(f'Only http and https web addresses can be fetched: {url}')
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise requests.ConnectionError(f"Couldn't find {parts.hostname}: {e}") from e
    for info in infos:
        address = ipaddress.ip_address(info[4][0].split('%', 1)[0])
        if not address.is_global:
            raise UnsafeURL(f"{parts.hostname} is a private or local address, which can't be fetched.")


def get(url, **kwargs):
    """requests.get, refusing non-public destinations at every redirect."""
    kwargs['allow_redirects'] = False
    for _ in range(MAX_REDIRECTS + 1):
        _check(url)
        response = requests.get(url, **kwargs)
        if not response.is_redirect:
            return response
        url = urljoin(url, response.headers['Location'])
    raise requests.TooManyRedirects(f'More than {MAX_REDIRECTS} redirects.')
