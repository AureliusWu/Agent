from enum import StrEnum


class TaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_CONFIRMATION = "waiting_confirmation"
    WAITING_PROVIDER = "waiting_provider"
    WAITING_PROVIDER_CREDENTIAL = "waiting_provider_credential"
    COMPLETED = "completed"
    PARTIALLY_COMPLETED = "partially_completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    INTERRUPTED = "interrupted"
    BLOCKED = "blocked"


FINAL_TASK_STATUSES = {
    TaskStatus.COMPLETED,
    TaskStatus.PARTIALLY_COMPLETED,
    TaskStatus.FAILED,
    TaskStatus.CANCELLED,
    TaskStatus.BLOCKED,
}


RESUMABLE_TASK_STATUSES = {
    TaskStatus.WAITING_CONFIRMATION,
    TaskStatus.WAITING_PROVIDER,
    TaskStatus.WAITING_PROVIDER_CREDENTIAL,
    TaskStatus.INTERRUPTED,
    TaskStatus.TIMED_OUT,
}
