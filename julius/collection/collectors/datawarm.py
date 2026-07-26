"""Identifica publicações DataWarm a partir do job configurado e sua linhagem."""

from __future__ import annotations

from julius.collection.models import Account


def mark_publications(account: Account, job_identifier: str) -> int:
    if not job_identifier:
        return 0
    identifiers = {
        job.name
        for job in account.glue_jobs
        if job.name == job_identifier or job_identifier.lower() in job.name.lower()
    }
    marked = 0
    for table in account.tables:
        if table.written_by in identifiers:
            table.datawarm_published = True
            writer = account.job_by_name(table.written_by)
            if writer and writer.owner_tag and not table.datawarm_owner:
                table.datawarm_owner = writer.owner_tag
            marked += 1
    return marked
