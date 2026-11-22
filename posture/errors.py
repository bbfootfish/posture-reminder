"""Errors shared by the HTTP API and the service."""

from __future__ import annotations


class ServiceBusy(RuntimeError):
    """A calibration or audit is already running."""


class ServiceUnavailable(RuntimeError):
    """The stream/model required for this operation is not available."""


class CalibrationTimeout(RuntimeError):
    """The camera did not produce enough frames in time."""
