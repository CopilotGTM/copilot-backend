from typing import Dict, Any

import requests
from werkzeug.exceptions import BadRequest


def _request(
    method: str,
    url: str,
    timeout=60,
    params: Dict[str, Any] = None,
    headers: Dict[str, Any] = None,
    payload: Dict[str, Any] = None,
    data: Dict[str, Any] = None,  # Deprecated
):
    resp = requests.request(method, url, params=params, headers=headers, json=payload, timeout=timeout, data=data)
    if 400 <= resp.status_code < 500:
        raise BadRequest(resp.text, resp)

    if resp.status_code == 204:
        return None

    if not 200 <= resp.status_code < 300:
        raise IOError(resp, resp)
    resp_json = resp.json()
    return resp_json
