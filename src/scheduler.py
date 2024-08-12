from datetime import datetime
import heapq
from typing import Dict, List, Optional
from dataclasses import dataclass, field


@dataclass(order=True)
class SyncTask:
    scheduled_time: datetime
    path_key: str = field(compare=False)


class SyncScheduler:
    def __init__(self):
        self.tasks: List[SyncTask] = []
        self.task_map: Dict[str, SyncTask] = {}

    def schedule_task(self, path_key: str, scheduled_time: datetime):
        if path_key in self.task_map:
            self.remove_task(path_key)
        task = SyncTask(scheduled_time, path_key)
        heapq.heappush(self.tasks, task)
        self.task_map[path_key] = task

    def remove_task(self, path_key: str):
        if path_key in self.task_map:
            task = self.task_map.pop(path_key)
            self.tasks.remove(task)
            heapq.heapify(self.tasks)

    def get_next_task(self) -> Optional[SyncTask]:
        return self.tasks[0] if self.tasks else None

    def pop_next_task(self) -> Optional[SyncTask]:
        if self.tasks:
            task = heapq.heappop(self.tasks)
            del self.task_map[task.path_key]
            return task
        return None


scheduler = SyncScheduler()
