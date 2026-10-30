"""Minimal OData client for Acumatica Generic Inquiries (read-only).

Uses the same endpoint style as westridgeApp's helper ({base}/odata/{tenant}/{gi})
with basic auth, but requests JSON and pages through results with $top/$skip.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterator, Optional
from urllib.parse import quote

import requests

log = logging.getLogger('acumatica')

DEFAULT_URL_TEMPLATE = '{base}/odata/{tenant}/{gi}'


class AcumaticaError(Exception):
    pass


class AcumaticaConfigError(AcumaticaError):
    pass


class AcumaticaRequestError(AcumaticaError):
    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class AcumaticaConfig:
    base_url: str
    tenant: str
    username: str
    password: str
    timeout: int = 60
    page_size: int = 1000
    url_template: str = DEFAULT_URL_TEMPLATE

    def validate(self) -> None:
        missing = [name for name in ('base_url', 'tenant', 'username', 'password') if not getattr(self, name)]
        if missing:
            raise AcumaticaConfigError(
                'Acumatica is not configured; set ' + ', '.join(f'ACUMATICA_{m.upper()}' for m in missing))


class ODataClient:
    def __init__(self, config: AcumaticaConfig, session: Optional[requests.Session] = None):
        config.validate()
        self.config = config
        self.session = session or requests.Session()
        self.session.auth = (config.username, config.password)
        self.session.headers.update({'Accept': 'application/json'})

    def gi_url(self, gi_name: str) -> str:
        return self.config.url_template.format(
            base=self.config.base_url.rstrip('/'),
            tenant=quote(self.config.tenant, safe=''),
            gi=quote(gi_name, safe=''),
        )

    def iter_rows(self, gi_name: str, select: Optional[list[str]] = None, filter: str = '') -> Iterator[dict[str, Any]]:
        """Yield every row of a Generic Inquiry, one page at a time."""
        url = self.gi_url(gi_name)
        skip = 0
        while True:
            params = {'$format': 'json', '$top': str(self.config.page_size), '$skip': str(skip)}
            if select:
                params['$select'] = ','.join(select)
            if filter:
                params['$filter'] = filter
            rows = self._get_page(url, params, gi_name)
            yield from rows
            if len(rows) < self.config.page_size:
                return
            skip += len(rows)

    def fetch_rows(self, gi_name: str, select: Optional[list[str]] = None, filter: str = '') -> list[dict[str, Any]]:
        return list(self.iter_rows(gi_name, select=select, filter=filter))

    def _get_page(self, url: str, params: dict[str, str], gi_name: str) -> list[dict[str, Any]]:
        try:
            response = self.session.get(url, params=params, timeout=self.config.timeout)
        except requests.RequestException as exc:
            raise AcumaticaRequestError(f'GI {gi_name!r}: request failed: {exc}') from exc
        if response.status_code in (401, 403):
            raise AcumaticaRequestError(f'GI {gi_name!r}: not authorised (check the service user and GI access rights)',
                                        response.status_code)
        if response.status_code == 404:
            raise AcumaticaRequestError(f'GI {gi_name!r}: not found (is it exposed via OData?)', 404)
        if response.status_code != 200:
            raise AcumaticaRequestError(f'GI {gi_name!r}: HTTP {response.status_code}: {response.text[:300]}',
                                        response.status_code)
        try:
            payload = response.json()
        except ValueError as exc:
            raise AcumaticaRequestError(f'GI {gi_name!r}: response was not JSON') from exc
        # OData v3 JSON light: {"value": [...]}; verbose: {"d": {"results": [...]}} or {"d": [...]}
        if isinstance(payload, dict) and 'value' in payload:
            rows = payload['value']
        elif isinstance(payload, dict) and 'd' in payload:
            d = payload['d']
            rows = d.get('results', []) if isinstance(d, dict) else d
        else:
            raise AcumaticaRequestError(f'GI {gi_name!r}: unexpected OData payload shape')
        log.debug('GI %s: %d rows (skip=%s)', gi_name, len(rows), params['$skip'])
        return rows
