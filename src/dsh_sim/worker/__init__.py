"""dsh-sim Worker 层（Agent E / WP-07）：领取循环、事件 WAL、适配器分发。"""
from dsh_sim.worker.loop import (
    WorkerConfig,
    make_adapter_from_env,
    replay_wal,
    run_until_idle,
)
from dsh_sim.worker.wal import JsonlWal

__all__ = [
    "JsonlWal",
    "WorkerConfig",
    "make_adapter_from_env",
    "replay_wal",
    "run_until_idle",
]
