from enum import StrEnum


class TaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_CONFIRMATION = "waiting_confirmation"
    COMPLETED = "completed"
    PARTIALLY_COMPLETED = "partially_completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    INTERRUPTED = "interrupted"
    PAUSED = "paused"
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
    TaskStatus.INTERRUPTED,
    TaskStatus.PAUSED,
    TaskStatus.TIMED_OUT,
}
