"""pytest 公共 fixture（mock 层）：SQLite 临时文件库 + FastAPI TestClient。

不污染 var/：每测试用例独立 tmp_path 数据库与 artifact 根目录。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from dsh_sim.api.main import create_app
from dsh_sim.db.session import init_db, make_engine, make_session_factory

pytestmark = pytest.mark.mock

EXECUTOR_HEADERS = {
    "X-Dev-Subject": "eng_zhang",
    "X-Dev-Roles": "EXECUTOR",
    "X-Dev-Projects": "proj_a",
}
REVIEWER_HEADERS = {
    "X-Dev-Subject": "rev_li",
    "X-Dev-Roles": "REVIEWER",
    "X-Dev-Projects": "proj_a",
}
AGENT_HEADERS = {
    "X-Dev-Subject": "agent_bot",
    "X-Dev-Roles": "AGENT",
    "X-Dev-Projects": "proj_a",
}
NODE_HEADERS = {
    "X-Dev-Subject": "node_svc",
    "X-Dev-Roles": "NODE_ADMIN",
    "X-Dev-Projects": "proj_a",
}
OTHER_PROJECT_HEADERS = {
    "X-Dev-Subject": "eng_wang",
    "X-Dev-Roles": "EXECUTOR",
    "X-Dev-Projects": "proj_b",
}

FAKE_SHA = "a" * 64
FAKE_SHA_B = "b" * 64


def make_spec(**overrides) -> dict:
    """合法 TaskSpec（结构合法层；模板/能力包引用为占位哈希）。"""
    spec = {
        "purpose": "design_screening",
        "variants": [
            {
                "variant_id": "A",
                "template_artifact_id": "art_template_a",
                "template_sha256": FAKE_SHA,
                "boundary_map_sha256": FAKE_SHA_B,
            },
            {
                "variant_id": "B",
                "template_artifact_id": "art_template_b",
                "template_sha256": FAKE_SHA_B,
                "boundary_map_sha256": FAKE_SHA_B,
            },
        ],
        "conditions": [
            {
                "condition_id": "C1",
                "fields": [
                    {
                        "role_id": "inlet",
                        "field": "total_pressure",
                        "quantity": {
                            "si_value": 101325.0,
                            "unit": "Pa",
                            "physical_meaning": "inlet_total_pressure",
                            "source_ref": "spec-sheet-001",
                            "pressure_kind": "absolute",
                            "pressure_semantics": "total",
                        },
                    }
                ],
            }
        ],
        "execution_budget": {
            "max_concurrent": 2,
            "cpu_cores": 8,
            "memory_gb": 16.0,
            "wallclock_hours": 4.0,
            "max_attempts_total": 4,
        },
        "method": {
            "capability_package_id": "buffer_chamber",
            "capability_package_sha256": FAKE_SHA,
            "required_metrics": ["mass_imbalance", "total_pressure_loss"],
            "review_scope": "A/B 方案筛选，三组工况内",
        },
    }
    spec.update(overrides)
    return spec


@pytest.fixture()
def engine(tmp_path):
    eng = make_engine(f"sqlite:///{(tmp_path / 'test.db').as_posix()}")
    init_db(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def session(engine) -> Session:
    factory = make_session_factory(engine)
    sess = factory()
    yield sess
    sess.rollback()
    sess.close()


@pytest.fixture()
def client(tmp_path) -> TestClient:
    app = create_app(
        database_url=f"sqlite:///{(tmp_path / 'api.db').as_posix()}",
        artifact_root=tmp_path / "artifacts",
        # 显式声明本地开发模式：身份模式是 fail-closed 的，缺省（或生产模式）会拒绝
        # X-Dev-* 自报身份头。测试必须像真实本地环境一样显式开启。
        identity_mode="dev",
    )
    with TestClient(app) as c:
        yield c
