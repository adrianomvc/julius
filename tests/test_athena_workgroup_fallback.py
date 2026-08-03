"""Descoberta e fallback dos workgroups conhecidos da conta."""

from julius.collection.collectors.athena.executions import workgroups
from julius.collection.collectors.athena.telemetry import AthenaTelemetry
from julius.collection.models import AthenaCoverage


class _DeniedListing:
    def get_paginator(self, _name):
        raise PermissionError("sem athena:ListWorkGroups")

    def get_work_group(self, *, WorkGroup):
        return {"WorkGroup": {"Name": WorkGroup, "Configuration": {}}}


class _Paginator:
    def paginate(self):
        return [{"WorkGroups": [{"Name": "discovered-fourth"}]}]


class _WorkingListing(_DeniedListing):
    def get_paginator(self, _name):
        return _Paginator()


KNOWN = ("primary", "analytics-workgroup", "analytics-workgroup-v3")
ROLES = {
    "primary": "unused_expected",
    "analytics-workgroup": "legacy",
    "analytics-workgroup-v3": "preferred",
}


def test_denied_listing_collects_known_workgroups_but_stays_partial() -> None:
    coverage = AthenaCoverage()
    telemetry = AthenaTelemetry(coverage)

    names, configs = workgroups(
        _DeniedListing(),
        coverage,
        telemetry,
        configured=KNOWN,
        configured_roles=ROLES,
    )

    assert names == list(KNOWN)
    assert set(configs) == set(KNOWN)
    assert coverage.workgroups_discovery_complete is False
    assert coverage.workgroups_total == 3
    assert coverage.workgroup_roles == ROLES
    assert telemetry.blocked() is True
    entry = telemetry.entries()[0]
    assert entry.status == "partial"
    assert entry.error_category == "permission_denied"


def test_discovery_is_unioned_with_known_workgroups() -> None:
    coverage = AthenaCoverage()
    telemetry = AthenaTelemetry(coverage)

    names, _ = workgroups(
        _WorkingListing(),
        coverage,
        telemetry,
        configured=KNOWN,
        configured_roles=ROLES,
    )

    assert names == ["discovered-fourth", *KNOWN]
    assert coverage.workgroups_discovery_complete is True
    assert coverage.workgroups_total == 4
    assert coverage.workgroup_roles["discovered-fourth"] == "unclassified"
    assert coverage.workgroup_roles["analytics-workgroup-v3"] == "preferred"
