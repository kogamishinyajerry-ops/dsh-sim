#!/usr/bin/env python3
"""生成 OpenFOAM worker 的模板注册表 JSON（每次调用生成一个模板并合并）。

DSH_SIM_OPENFOAM_TEMPLATE_REGISTRY 需要【JSON 文件路径】，内容为
{"引用名": "/abs/path/template.tar"}；每个路径必须位于
DSH_SIM_OPENFOAM_TEMPLATE_ROOT 之内（即本脚本的 --root）。模板由
make_channel_template 确定性生成（参数即求解输入；工程阈值仍为 Owner
冻结项，与本脚本无关）。TaskSpec 侧需要记录输出的 template_sha256 与
boundary_map_sha256。

用法（可对不同 --ref 重复调用，registry.json 自动合并）：
  python make-template-registry.py --root /opt/data/openfoam-templates \
      --ref public-openfoam-acc-01 \
      --mean-velocity 0.012 --length 1.2 --height 0.08 --width 0.012 \
      --nu 0.0012 --density 1050 --nx 72 --ny 16 --iterations 600
"""
from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path

from dsh_sim.adapters.openfoam_adapter import (
    make_channel_template,
    read_template_metadata,
    template_sha256,
)

# 引用名安全白名单：字母/数字开头，仅字母数字._-，禁正斜杠与 ..
# （防止 ref 构造出路径穿越，如 ../../etc/x 或绝对路径片段）
REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def validate_ref(ref: str) -> None:
    if not REF_RE.fullmatch(ref):
        raise ValueError(
            f"invalid ref {ref!r}: must match {REF_RE.pattern} (no '/', no '..')")
    if ".." in ref:
        raise ValueError(f"invalid ref {ref!r}: '..' is not allowed")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", required=True, help="模板与 registry.json 的根目录（=TEMPLATE_ROOT）")
    ap.add_argument("--ref", required=True, help="模板引用名（TaskSpec.template_artifact_id）")
    ap.add_argument("--mean-velocity", type=float, required=True)
    ap.add_argument("--length", type=float, required=True)
    ap.add_argument("--height", type=float, required=True)
    ap.add_argument("--width", type=float, required=True)
    ap.add_argument("--nu", type=float, required=True)
    ap.add_argument("--density", type=float, required=True)
    ap.add_argument("--nx", type=int, required=True)
    ap.add_argument("--ny", type=int, required=True)
    ap.add_argument("--iterations", type=int, required=True)
    args = ap.parse_args()

    # ---- 写入前全部校验：失败不产生任何文件、不污染 registry ----
    validate_ref(args.ref)
    root = Path(args.root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    dest = (root / f"channel-{args.ref}.tar").resolve()
    if not dest.is_relative_to(root):
        raise ValueError(f"resolved destination {dest} escapes root {root}")
    registry_path = root / "registry.json"
    registry = {}
    if registry_path.exists():
        try:
            registry = json.loads(registry_path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise ValueError(f"existing registry.json is not valid JSON: {exc}") from exc

    params = {
        "mean_velocity": args.mean_velocity, "length": args.length, "height": args.height,
        "width": args.width, "nu": args.nu, "density": args.density,
        "nx": args.nx, "ny": args.ny, "iterations": args.iterations,
    }

    # ---- 校验全部通过后才写模板；最后原子写 registry（tmp + replace）----
    path = make_channel_template(dest, **params)
    registry[args.ref] = str(path)
    tmp = registry_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(registry, indent=1, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, registry_path)

    print(json.dumps({
        "ref": args.ref, "path": str(path), "sha256": template_sha256(path),
        "boundary_map_sha256": read_template_metadata(path)["boundary_map_sha256"],
        "parameters": params, "registry": str(registry_path),
    }, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
