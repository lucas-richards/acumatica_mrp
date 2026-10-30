"""Read-only client for Acumatica's contract-based REST API.

Logs in with a cookie session, pages through entities with $top/$skip and always
logs out (each open session uses one of the instance's API logins). Entities and
Generic Inquiries (OData) are only read with GET; nothing is ever written.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterator, Optional
from urllib.parse import quote

import requests

log = logging.getLogger('acumatica')


class AcumaticaError(Exception):
    pass


class AcumaticaConfigError(AcumaticaError):
    pass


class AcumaticaRequestError(AcumaticaError):
    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


class AcumaticaPermissionError(AcumaticaRequestError):
    pass


@dataclass(frozen=True)
class AcumaticaConfig:
    base_url: str
    tenant: str
    username: str
    password: str
    branch: str = ''
    endpoints: tuple[tuple[str, str], ...] = (('default', 'Default/24.200.001'),
                                              ('manufacturing', 'MANUFACTURING/24.200.001'))
    available_gi: str = 'LR-Available Qty'
    timeout: int = 120
    page_size: int = 250

    def validate(self) -> None:
        missing = [name for name in ('base_url', 'tenant', 'username', 'password') if not getattr(self, name)]
        if missing:
            raise AcumaticaConfigError(
                'Acumatica is not configured; set ' + ', '.join(f'ACUMATICA_{m.upper()}' for m in missing))

    def endpoint(self, key: str) -> str:
        return dict(self.endpoints)[key].strip('/')


def flatten(value: Any) -> Any:
    """Turn Acumatica's {"Field": {"value": x}} shape into plain {"Field": x}."""
    if isinstance(value, list):
        return [flatten(v) for v in value]
    if isinstance(value, dict):
        if set(value) <= {'value', 'error'} and 'value' in value:
            return value['value']
        if not value:   # a field with no value comes back as {}
            return None
        return {k: flatten(v) for k, v in value.items() if k not in ('_links', 'custom', 'note', 'rowNumber')}
    return value


class RestClient:
    """Use as a context manager so the session is always logged out."""

    def __init__(self, config: AcumaticaConfig, session: Optional[requests.Session] = None):
        config.validate()
        self.config = config
        self.session = session or requests.Session()
        self.session.headers.update({'Accept': 'application/json'})
        self._logged_in = False

    def __enter__(self) -> 'RestClient':
        self.login()
        return self

    def __exit__(self, *exc) -> None:
        self.logout()

    def _url(self, path: str) -> str:
        return self.config.base_url.rstrip('/') + path

    def login(self) -> None:
        body = {'name': self.config.username, 'password': self.config.password, 'company': self.config.tenant}
        if self.config.branch:
            body['branch'] = self.config.branch
        try:
            r = self.session.post(self._url('/entity/auth/login'), json=body, timeout=self.config.timeout)
        except requests.RequestException as exc:
            raise AcumaticaRequestError(f'Login request failed: {exc}') from exc
        if r.status_code not in (200, 204):
            raise AcumaticaRequestError(f'Login failed (HTTP {r.status_code}): {_message(r)}', r.status_code)
        self._logged_in = True

    def logout(self) -> None:
        if not self._logged_in:
            return
        try:
            self.session.post(self._url('/entity/auth/logout'), timeout=30)
        except requests.RequestException as exc:   # never mask the real error
            log.warning('Acumatica logout failed: %s', exc)
        self._logged_in = False

    def iter_entities(self, endpoint: str, entity: str, *, select: Optional[list[str]] = None,
                      expand: Optional[list[str]] = None, filter: str = '') -> Iterator[dict[str, Any]]:
        url = self._url(f'/entity/{self.config.endpoint(endpoint)}/{entity}')
        skip = 0
        while True:
            params = {'$top': str(self.config.page_size), '$skip': str(skip)}
            if select:
                params['$select'] = ','.join(select)
            if expand:
                params['$expand'] = ','.join(expand)
            if filter:
                params['$filter'] = filter
            rows = self._get(url, params, entity)
            for row in rows:
                yield flatten(row)
            if len(rows) < self.config.page_size:
                return
            skip += len(rows)

    def generic_inquiry(self, name: str, select: Optional[list[str]] = None) -> list[dict[str, Any]]:
        """Read a Generic Inquiry over OData ({base}/odata/{tenant}/{name}) with a GET."""
        url = self._url(f'/odata/{quote(self.config.tenant, safe="")}/{quote(name, safe="")}')
        params = {'$format': 'json'}
        if select:
            params['$select'] = ','.join(select)
        try:
            r = self.session.get(url, params=params, auth=(self.config.username, self.config.password),
                                 timeout=self.config.timeout)
        except requests.RequestException as exc:
            raise AcumaticaRequestError(f'GI {name!r}: request failed: {exc}') from exc
        if r.status_code in (401, 403):
            raise AcumaticaPermissionError(f'GI {name!r}: not authorised', r.status_code)
        if r.status_code == 404:
            raise AcumaticaRequestError(f'GI {name!r} not found (is it exposed via OData?)', 404)
        if r.status_code != 200:
            raise AcumaticaRequestError(f'GI {name!r}: HTTP {r.status_code}: {_message(r)}', r.status_code)
        try:
            rows = r.json().get('value', [])
        except (ValueError, AttributeError) as exc:
            raise AcumaticaRequestError(f'GI {name!r}: unexpected response') from exc
        log.info('Fetched GI %s: %d rows', name, len(rows))
        return rows

    def fetch(self, endpoint: str, entity: str, **kwargs) -> list[dict[str, Any]]:
        rows = list(self.iter_entities(endpoint, entity, **kwargs))
        log.info('Fetched %s: %d records', entity, len(rows))
        return rows

    def _get(self, url: str, params: dict[str, str], entity: str) -> list:
        try:
            r = self.session.get(url, params=params, timeout=self.config.timeout)
        except requests.RequestException as exc:
            raise AcumaticaRequestError(f'{entity}: request failed: {exc}') from exc
        if r.status_code in (401, 403):
            raise AcumaticaPermissionError(f'{entity}: {_message(r)}', r.status_code)
        if r.status_code != 200:
            raise AcumaticaRequestError(f'{entity}: HTTP {r.status_code}: {_message(r)}', r.status_code)
        try:
            body = r.json()
        except ValueError as exc:
            raise AcumaticaRequestError(f'{entity}: response was not JSON') from exc
        if not isinstance(body, list):
            raise AcumaticaRequestError(f'{entity}: expected a list of records')
        return body


def _message(response) -> str:
    try:
        body = response.json()
        return body.get('exceptionMessage') or body.get('message') or str(body)[:300]
    except ValueError:
        return response.text[:300]
