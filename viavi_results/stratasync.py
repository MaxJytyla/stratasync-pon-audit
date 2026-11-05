"""Client for the StrataSync TPA Test Results JSON API (v1).

Reference: "StrataSync TPA Test Results JSON API Developer Guide" (May 2023).

Auth: POST {base_url}/oauth/token with client credentials returns a bearer
token (valid ~15 minutes; typically only one valid token per client ID, so we
cache and reuse it). Search: POST {base_url}/api/v1/testresults/search.

Search constraints from the guide:
  * createdOn or syncTime is required unless filtering by workflowId or _id.
  * Each date range must cover at most a 24-hour period, so multi-day
    queries are split into one request per day.
  * Response fields vary by test type and firmware; never assume a fixed set.
"""

from __future__ import annotations

import gzip
import json
import threading
import time
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence, Union

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import StrataSyncSettings

TOKEN_PATH = "/oauth/token"
SEARCH_PATH = "/api/v1/testresults/search"
JOB_SEARCH_PATH = "/api/v3/workflow/search"
JOB_CREATE_PATH = "/api/v3/workflow/workorder"
JOB_PATH = "/api/v3/workflow/{workflow_id}"
# The web UI's internal endpoint for unapproving a job (APPROVED back to
# COMPLETED). Not documented; the public API can only approve. The TPA token
# was accepted for it (job_manager, checked 2026-11-03).
JOB_UNAPPROVE_PATH = "/api/internal/v1/workflow/{workflow_id}/unapprove"

# A job's Cable ID attribute as the RMN templates define it, for a job that
# doesn't have one yet.
CABLE_ID_ATTRIBUTE = {"orderIndex": 0, "label": "Cable ID", "valueType": "string", "editable": "true",
                      "visible": True}

# Values for DateRange.dateFilter (guide p.22).
TODAY = -1
TOMORROW = -2
YESTERDAY = -3
PREV_1_DAY = -61

# Date fields that can be searched on.
CREATED_ON = "createdOn"
SYNC_TIME = "syncTime"

# StrataSync rejects a respFields list that omits these.
REQUIRED_RESP_FIELDS = ("assetType", "result")

# Refresh the token this many seconds before it actually expires.
_TOKEN_MARGIN_S = 60

# Longest span one search may cover (the API allows at most 24 hours).
_MAX_WINDOW = timedelta(hours=24) - timedelta(seconds=1)
# Longest span one job search may cover (the Jobs API allows at most 35 days).
_MAX_JOB_WINDOW = timedelta(days=35) - timedelta(seconds=1)

DateLike = Union[str, date, datetime]


class StrataSyncError(RuntimeError):
    """Raised when the API returns an error or an unexpected response."""

    def __init__(self, message: str, status: Optional[int] = None, body: Any = None):
        super().__init__(message)
        self.status = status
        self.body = body


def _fmt_date(value: DateLike) -> str:
    """Format a date/datetime as the ISO 8601 string the API expects."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("datetime values must be timezone-aware (API needs a TZD)")
        return value.isoformat(timespec="seconds")
    if isinstance(value, date):
        return value.isoformat()
    return value


def date_range(
    start: Optional[DateLike] = None,
    end: Optional[DateLike] = None,
    date_filter: Optional[int] = None,
) -> Dict[str, Any]:
    """Build a DateRange object. `date_filter` takes precedence over start/end."""
    if date_filter is not None:
        return {"dateFilter": date_filter}
    if start is None and end is None:
        raise ValueError("Provide start and/or end, or a date_filter")
    rng: Dict[str, Any] = {}
    if start is not None:
        rng["startDate"] = _fmt_date(start)
    if end is not None:
        rng["endDate"] = _fmt_date(end)
    return rng


class StrataSyncClient:
    def __init__(self, settings: StrataSyncSettings, timeout: float = 60.0):
        self._settings = settings
        self._timeout = timeout
        self._token: Optional[str] = settings.access_token
        # A token supplied directly has an unknown expiry; treat it as valid
        # until the server says otherwise.
        self._token_expires_at = float("inf") if self._token else 0.0
        # The client may be shared by download threads. Only one valid token
        # exists per client ID, so token renewal is serialized: otherwise each
        # thread's renewal would cancel the token the others are using.
        self._token_lock = threading.Lock()

        self._session = requests.Session()
        retry = Retry(
            total=3,
            backoff_factor=2,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET", "POST"}),  # search is read-only
            respect_retry_after_header=True,
        )
        self._session.mount("https://", HTTPAdapter(max_retries=retry, pool_maxsize=16))
        # Changes (creating jobs) are sent once and never retried automatically.
        self._write_session = requests.Session()

    # ---- auth -------------------------------------------------------------

    @property
    def _can_refresh(self) -> bool:
        return bool(self._settings.client_id and self._settings.client_secret)

    def request_token(self) -> Dict[str, Any]:
        """Request a new access token with the client credentials.

        Returns the raw token response (access_token, token_type, expires_in,
        scope) and makes the new token the one this client uses.
        """
        if not self._can_refresh:
            raise StrataSyncError(
                "A client ID and client secret are required to request a token"
            )
        resp = self._session.post(
            self._settings.base_url + TOKEN_PATH,
            data={
                "grant_type": "client_credentials",
                "scope": "",
                "client_id": self._settings.client_id,
                "client_secret": self._settings.client_secret,
            },
            headers={"Accept": "application/json"},
            timeout=self._timeout,
        )
        if resp.status_code != 200:
            raise StrataSyncError(
                f"Token request failed: HTTP {resp.status_code}",
                status=resp.status_code,
                body=resp.text,
            )
        try:
            body = resp.json()
            token = body["access_token"]
        except (ValueError, KeyError, TypeError):
            raise StrataSyncError(
                "Token response did not contain an access_token", status=200, body=resp.text
            )
        self._token = token
        expires_in = float(body.get("expires_in", 900))
        self._token_expires_at = time.monotonic() + max(expires_in - _TOKEN_MARGIN_S, 0)
        return body

    def _bearer(self, force_refresh: bool = False, rejected: Optional[str] = None) -> str:
        """The current token, renewed if expired. `force_refresh` (after a 401)
        renews it only if `rejected` is still the current token; if another
        thread already renewed it, the new one is reused."""
        with self._token_lock:
            expired = time.monotonic() >= self._token_expires_at
            still_rejected = force_refresh and (rejected is None or rejected == self._token)
            if self._can_refresh and (still_rejected or expired or not self._token):
                self.request_token()
            if not self._token:
                raise StrataSyncError("No access token available")
            return self._token

    # ---- search -----------------------------------------------------------

    def search(self, query: Dict[str, Any]) -> List[Dict[str, Any]]:
        """POST a raw search query and return the list of result records."""
        body = self._post_json(SEARCH_PATH, query)
        if body.get("type") != "success":
            raise StrataSyncError(
                f"Search failed: {body.get('message') or body}",
                status=body.get("httpStatus"),
                body=body,
            )
        return list(body.get("result") or [])

    def _post_json(self, path: str, payload: Optional[Dict[str, Any]],
                   session: Optional[requests.Session] = None,
                   params: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        return self._send_json("POST", path, payload, session, params)

    def _send_json(self, method: str, path: str, payload: Optional[Dict[str, Any]],
                   session: Optional[requests.Session] = None,
                   params: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        url = self._settings.base_url + path
        token = None
        for attempt in (1, 2):
            token = self._bearer(force_refresh=attempt == 2, rejected=token)
            # A 401 means nothing was done, so even a change can be resent after it.
            resp = (session or self._session).request(
                method,
                url,
                json=payload,
                params=params,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                },
                timeout=self._timeout,
            )
            # On 401, refresh the token once (if we can) and retry.
            if resp.status_code == 401 and attempt == 1 and self._can_refresh:
                continue
            break

        try:
            body = resp.json()
        except ValueError:
            body = None
        if resp.status_code != 200 or not isinstance(body, dict):
            message = (" ".join(filter(None, (body.get("message"), body.get("detailMessage"))))
                       if isinstance(body, dict) else resp.text[:500])
            hint = ""
            if resp.status_code == 401 and not self._can_refresh:
                hint = " (access token may have expired; tokens last ~15 minutes)"
            raise StrataSyncError(
                f"HTTP {resp.status_code} from {path}: {message}{hint}",
                status=resp.status_code,
                body=body if body is not None else resp.text,
            )
        return body

    def search_results(
        self,
        created_on: Optional[Dict[str, Any]] = None,
        sync_time: Optional[Dict[str, Any]] = None,
        *,
        test_type: Optional[str] = None,
        result: Optional[str] = None,
        work_order_id: Optional[str] = None,
        workflow_id: Optional[int] = None,
        tech_id: Optional[str] = None,
        unique_id: Optional[str] = None,
        asset_type: Optional[str] = None,
        name: Optional[str] = None,
        full_org_path: Optional[str] = None,
        file_format: Optional[str] = None,
        resp_fields: Optional[Sequence[str]] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """Search with named filters. Date ranges come from `date_range()`.

        `resp_fields` limits returned fields; the required ones are added
        automatically. `extra` passes any additional raw query keys.
        """
        if created_on is None and sync_time is None and workflow_id is None:
            raise ValueError("created_on or sync_time is required unless workflow_id is given")

        query: Dict[str, Any] = {}
        optional = {
            CREATED_ON: created_on,
            SYNC_TIME: sync_time,
            "type": test_type,
            "result": result,
            "workOrderId": work_order_id,
            "workflowId": int(workflow_id) if workflow_id is not None else None,
            "techId": tech_id,
            "uniqueId": unique_id,
            "assetType": asset_type,
            "name": name,
            "fullOrgPath": full_org_path,
            "format": file_format,
        }
        query.update({k: v for k, v in optional.items() if v is not None})
        if resp_fields is not None:
            fields = list(resp_fields)
            if fields:
                fields += [f for f in REQUIRED_RESP_FIELDS if f not in fields]
            query["respFields"] = fields
        if extra:
            query.update(extra)
        return self.search(query)

    def search_days(
        self,
        start: date,
        end: date,
        *,
        by: str = CREATED_ON,
        **filters: Any,
    ) -> List[Dict[str, Any]]:
        """Search every day from `start` to `end` inclusive (UTC days).

        The API only accepts 24-hour windows, so this issues one request per
        day and de-duplicates results by `_id`.
        """
        if by not in (CREATED_ON, SYNC_TIME):
            raise ValueError(f"by must be {CREATED_ON!r} or {SYNC_TIME!r}")
        if end < start:
            raise ValueError("end must not be before start")

        seen: Dict[Any, Dict[str, Any]] = {}
        for day in _days(start, end):
            rng = date_range(day, day)
            kwargs = {"created_on": rng} if by == CREATED_ON else {"sync_time": rng}
            for rec in self.search_results(**kwargs, **filters):
                seen.setdefault(rec.get("_id"), rec)
        return list(seen.values())

    def search_workflow(self, workflow_id: int, **filters: Any) -> List[Dict[str, Any]]:
        """All results for one workflow ID (no date range needed)."""
        return self.search_results(workflow_id=workflow_id, **filters)

    def search_between(
        self,
        start: datetime,
        end: datetime,
        *,
        by: str = SYNC_TIME,
        **filters: Any,
    ) -> List[Dict[str, Any]]:
        """Search an arbitrary time span, split into windows under 24 hours.

        Both ends are inclusive and must be timezone-aware. Results are
        de-duplicated by `_id`.
        """
        if by not in (CREATED_ON, SYNC_TIME):
            raise ValueError(f"by must be {CREATED_ON!r} or {SYNC_TIME!r}")
        if start.tzinfo is None or end.tzinfo is None:
            raise ValueError("start and end must be timezone-aware")
        seen: Dict[Any, Dict[str, Any]] = {}
        window_start = start
        while window_start <= end:
            window_end = min(window_start + _MAX_WINDOW, end)
            rng = date_range(window_start, window_end)
            kwargs = {"created_on": rng} if by == CREATED_ON else {"sync_time": rng}
            for rec in self.search_results(**kwargs, **filters):
                seen.setdefault(rec.get("_id"), rec)
            window_start = window_end + timedelta(seconds=1)
        return list(seen.values())

    # ---- download ---------------------------------------------------------

    def download_contents(self, test_id: Union[int, str]) -> bytes:
        """Download the raw test data file for a search result's `_id`."""
        path = f"/api/v1/testresults/{int(test_id)}/contents"
        token = None
        for attempt in (1, 2):
            token = self._bearer(force_refresh=attempt == 2, rejected=token)
            resp = self._session.get(
                self._settings.base_url + path,
                headers={"Authorization": f"Bearer {token}"},
                timeout=self._timeout,
            )
            if resp.status_code == 401 and attempt == 1 and self._can_refresh:
                continue
            break
        if resp.status_code != 200:
            raise StrataSyncError(
                f"HTTP {resp.status_code} downloading test {test_id}: {resp.text[:300]}",
                status=resp.status_code,
                body=resp.text,
            )
        return resp.content

    def download_json(self, test_id: Union[int, str]) -> Dict[str, Any]:
        """Download a JSON test data file (e.g. a TruePON .wfl.json) and parse it."""
        content = self.download_contents(test_id)
        try:
            return json.loads(content)
        except ValueError:
            # Some files are stored gzipped (format application/zip / .json.gz).
            return json.loads(gzip.decompress(content))


    # ---- jobs (TPA Jobs JSON API v3) ---------------------------------------
    #
    # Reference: "StrataSync TPA Jobs JSON API Developer Guide" (Feb 2025). The
    # same client credentials and token are used (only one token is valid per
    # client ID, so a second client would cancel this one's).

    def search_jobs(self, start: datetime, end: datetime) -> List[Dict[str, Any]]:
        """Every job scheduled from `start` to `end` (timezone-aware, inclusive).

        The API allows at most 35 days per search, so longer spans are split.
        Each job is a CDM 2.2 record with 'state', 'tests' and 'workflow'
        (workflowId, workOrderId, date). De-duplicated by workflowId.
        """
        if start.tzinfo is None or end.tzinfo is None:
            raise ValueError("start and end must be timezone-aware")
        seen: Dict[Any, Dict[str, Any]] = {}
        window_start = start
        while window_start <= end:
            window_end = min(window_start + _MAX_JOB_WINDOW, end)
            body = self._post_json(JOB_SEARCH_PATH, {"workflow.date": date_range(window_start, window_end)})
            jobs = (body.get("result") or {}).get("cdm") or []
            size = body.get("size")
            if isinstance(size, int) and size > len(jobs):
                raise StrataSyncError(f"Job search returned {len(jobs)} of {size} jobs", body=body)
            for job in jobs:
                seen.setdefault((job.get("workflow") or {}).get("workflowId"), job)
            window_start = window_end + timedelta(seconds=1)
        return list(seen.values())

    def create_job_from_template(
        self,
        work_order_id: str,
        template_name: str,
        scheduled: datetime,
        location_labels: Sequence[str],
        org_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Create a job from a StrataSync job template, assigned to no technician.

        The job is created in the STARTED state, in organization `org_id`
        (without it, StrataSync puts it in the account's root organization),
        and isn't sent to any meter until someone assigns it. The guide only
        documents 'dispatch' for full job definitions, but this request accepts
        it too (checked 2026-11-03). Returns the created job (its 'workflow'
        holds the new workflowId, and 'orgId' its organization).

        Sent once and never retried automatically: if the response is lost, a
        retry could create a second job. StrataSync itself refuses a second job
        with the same work order ID and scheduled date.
        """
        payload = {
            "workOrder": {
                "id": work_order_id,
                "templateName": template_name,
                "scheduledDate": _fmt_date(scheduled),
                **({"dispatch": {"orgId": int(org_id)}} if org_id else {}),
            },
            "testLocations": [{"label": label, "required": True} for label in location_labels],
        }
        body = self._post_json(JOB_CREATE_PATH, payload, session=self._write_session)
        if body.get("type") != "success":
            raise StrataSyncError(f"Job creation failed: {body.get('message') or body}",
                                  status=body.get("httpStatus"), body=body)
        return (body.get("result") or {}).get("cdm") or {}


    def set_cable_id(self, job: Dict[str, Any], cable_id: str) -> Dict[str, Any]:
        """Set the Cable ID of a job that isn't approved (e.g. one just created).

        `job` is the job as StrataSync returned it. Only the job's Job Manager
        attributes are sent, all of them with the Cable ID changed: the
        PATCH may only change techInfo, date, dueDate and those attributes,
        and changing the technician would reset the job and detach its
        results. Sent once, never retried. Returns the updated job.
        """
        workflow = job.get("workflow") or {}
        workflow_id = workflow.get("workflowId")
        if not workflow_id:
            raise StrataSyncError("The job has no workflowId, so its Cable ID can't be set")
        attributes = {k: dict(v) for k, v in (workflow.get("jobManagerAttributes") or {}).items()
                      if isinstance(v, dict)}
        attributes.setdefault("cableId", dict(CABLE_ID_ATTRIBUTE))["value"] = cable_id
        body = self._send_json("PATCH", JOB_PATH.format(workflow_id=int(workflow_id)),
                               {"cdmVersion": "2.2", "cdm": {"workflow": {"jobManagerAttributes": attributes}}},
                               session=self._write_session)
        if body.get("type") != "success":
            raise StrataSyncError(f"Setting the Cable ID failed: {body.get('message') or body}",
                                  status=body.get("httpStatus"), body=body)
        updated = (body.get("result") or {}).get("cdm") or {}
        got = (((updated.get("workflow") or {}).get("jobManagerAttributes") or {}).get("cableId") or {}).get("value")
        if updated and (got or "") != cable_id:
            raise StrataSyncError(f"StrataSync answered with Cable ID {got!r} instead of {cable_id!r}")
        return updated

    def approve_job(self, workflow_id: int) -> Dict[str, Any]:
        """Approve a COMPLETED (or INPROGRESS) job by its workflowId. Approved jobs
        are locked: they can't be modified until they're unapproved. Sent once,
        never retried. Returns the updated job."""
        body = self._post_json(JOB_PATH.format(workflow_id=int(workflow_id)), None,
                               session=self._write_session, params={"newState": "APPROVED"})
        if body.get("type") != "success":
            raise StrataSyncError(f"Approving the job failed: {body.get('message') or body}",
                                  status=body.get("httpStatus"), body=body)
        return (body.get("result") or {}).get("cdm") or {}

    def unapprove_job(self, workflow_id: int) -> Dict[str, Any]:
        """Set an APPROVED job back to COMPLETED, so it can be modified (see
        JOB_UNAPPROVE_PATH: an undocumented endpoint that could change). Sent
        once, never retried. Returns the updated job."""
        body = self._post_json(JOB_UNAPPROVE_PATH.format(workflow_id=int(workflow_id)), None,
                               session=self._write_session)
        if body.get("type") != "success":
            raise StrataSyncError(f"Unapproving the job failed: {body.get('message') or body}",
                                  status=body.get("httpStatus"), body=body)
        return (body.get("result") or {}).get("cdm") or {}


def _days(start: date, end: date) -> Iterable[date]:
    day = start
    while day <= end:
        yield day
        day += timedelta(days=1)
