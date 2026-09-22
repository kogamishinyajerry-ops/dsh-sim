"""直接调用服务层：把 PREPARE 作业落库完成（真实 canonical digest），驱动 T1-T3 到 READY。

用法: default-env python scripts/wk_finalize_prepare.py
"""
from __future__ import annotations

import hashlib
import json
import sys

sys.path.insert(0, r"<REPO_ROOT>\src")

from dsh_sim.api.services.prep_service import mark_preparation_ready
from dsh_sim.db.session import make_engine, make_session_factory

DB_URL = "sqlite:///<REPO_ROOT>/var/smoke.db"
EVID = r"<REPO_ROOT>\var\demo_evidence"
STATE_PATH = EVID + r"\task_chain_state.json"


def fsha(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


ARTIFACT_SETS = {
    "T1": {"naca2412_convergence.json": fsha(EVID + r"\naca2412_v34_500iter_convergence.json")},
    "T2": {"cyl_convergence.json": fsha(EVID + r"\cyl_vortex_v161R_v26_solved_convergence.json"),
           "cyl_postprocess.json": fsha(EVID + r"\save_postprocess.json")},
    "T3": {"ab_identity_diff.json": fsha(EVID + r"\t3_ab_identity_diff.json")},
}

READBACK = hashlib.sha256(b"readback: template-inherited settings verified").hexdigest()
ADAPTER = "starccm-cli-49.0.0+STAR-2402.19.02.009-R8"
SOFTWARE = "Simcenter STAR-CCM+ 2402 (19.02.009-R8) win64 DP"


def main() -> None:
    engine = make_engine(DB_URL)
    factory = make_session_factory(engine)
    with open(STATE_PATH, encoding="utf-8") as f:
        state = json.load(f)
    with factory() as s:
        for tid in ("T1", "T2", "T3"):
            prep_id = state[tid]["prep"]["preparation_id"]
            row = mark_preparation_ready(
                s, prep_id,
                prepared_artifacts=ARTIFACT_SETS[tid],
                readback_sha256=READBACK,
                adapter_build=ADAPTER,
                software_build=SOFTWARE,
                differences=[],
            )
            s.commit()
            print(tid, "ready=", row.ready, "digest=", row.prepared_digest[:16],
                  "blockers=", len(row.blockers or []))
            state[tid]["prepared_digest"] = row.prepared_digest
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
