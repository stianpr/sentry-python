from __future__ import absolute_import

import logging
import weakref

from sentry_sdk.hub import Hub
from sentry_sdk.integrations import Integration
from sentry_sdk.utils import capture_internal_exceptions, event_from_exception

from rq.timeouts import JobTimeoutException  # type: ignore
from rq.worker import Worker  # type: ignore

MYPY = False
if MYPY:
    from typing import Any
    from typing import Dict
    from typing import Callable

    from rq.job import Job  # type: ignore
    from rq.queue import Queue  # type: ignore

    from sentry_sdk.utils import ExcInfo


class RqIntegration(Integration):
    identifier = "rq"

    @staticmethod
    def setup_once():
        # type: () -> None

        old_perform_job = Worker.perform_job

        def sentry_patched_perform_job(self, job, *args, **kwargs):
            # type: (Any, Job, *Queue, **Any) -> bool
            hub = Hub.current
            integration = hub.get_integration(RqIntegration)

            logging.info("[RQIntegration] Hub.current: {}".format(hub))

            if integration is None:
                return old_perform_job(self, job, *args, **kwargs)

            client = hub.client
            assert client is not None

            logging.info("[RQIntegration] hub.client: {}".format(client))

            with hub.push_scope() as scope:
                scope._name = "rq"
                scope.clear_breadcrumbs()
                scope.add_event_processor(_make_event_processor(weakref.ref(job)))
                scope.user = {'id': 'prestholdt'}
                rv = old_perform_job(self, job, *args, **kwargs)

            print("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!! - inside patched job", scope)

            if self.is_horse:
                # We're inside of a forked process and RQ is
                # about to call `os._exit`. Make sure that our
                # events get sent out.
                client.flush()

            return rv

        Worker.perform_job = sentry_patched_perform_job

        old_handle_exception = Worker.handle_exception

        def sentry_patched_handle_exception(self, job, *exc_info, **kwargs):
            print("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!! - sentry_patched_handle_exception")

            _capture_exception(exc_info)  # type: ignore
            return old_handle_exception(self, job, *exc_info, **kwargs)

        Worker.handle_exception = sentry_patched_handle_exception


def _make_event_processor(weak_job):
    # type: (Callable[[], Job]) -> Callable
    def event_processor(event, hint):
        # type: (Dict[str, Any], Dict[str, Any]) -> Dict[str, Any]
        job = weak_job()
        if job is not None:
            with capture_internal_exceptions():
                event["transaction"] = job.func_name

            print("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!! - Add job stuff")

            with capture_internal_exceptions():
                extra = event.setdefault("extra", {})
                extra["rq-job"] = {
                    "job_id": job.id,
                    "func": job.func_name,
                    "args": job.args,
                    "kwargs": job.kwargs,
                    "description": job.description,
                }

        if "exc_info" in hint:
            with capture_internal_exceptions():
                if issubclass(hint["exc_info"][0], JobTimeoutException):
                    event["fingerprint"] = ["rq", "JobTimeoutException", job.func_name]

        return event

    return event_processor


def _capture_exception(exc_info, **kwargs):
    # type: (ExcInfo, **Any) -> None
    hub = Hub.current
    if hub.get_integration(RqIntegration) is None:
        return

    # If an integration is there, a client has to be there.
    client = hub.client  # type: Any

    event, hint = event_from_exception(
        exc_info,
        client_options=client.options,
        mechanism={"type": "rq", "handled": False},
    )

    print("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!! - Capture exception")

    hub.capture_event(event, hint=hint)
