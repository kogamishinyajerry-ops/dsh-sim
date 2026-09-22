"""capabilities 子包：能力包目录扫描/注册。"""
from dsh_sim.capabilities.registry import register_capabilities, scan_capabilities_root

__all__ = ["register_capabilities", "scan_capabilities_root"]
