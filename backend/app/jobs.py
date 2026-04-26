import logging

from .models import PlatformJob, db, utcnow


logger = logging.getLogger(__name__)


def enqueue_job(job_type, *, user=None, school_id=None, payload=None, scheduled_for=None):
    job = PlatformJob(
        school_id=school_id or getattr(user, 'school_scope_id', None),
        user_id=getattr(user, 'id', None),
        job_type=job_type,
        status='queued',
        payload=payload or {},
        scheduled_for=scheduled_for,
    )
    db.session.add(job)
    logger.info("Queued platform job type=%s school_id=%s user_id=%s", job_type, job.school_id, job.user_id)
    return job


def mark_job_running(job):
    job.status = 'running'
    job.started_at = utcnow()


def mark_job_complete(job, *, result=None):
    job.status = 'completed'
    job.result = result or {}
    job.completed_at = utcnow()


def mark_job_failed(job, *, error_message=None):
    job.status = 'failed'
    job.error_message = error_message
    job.completed_at = utcnow()
