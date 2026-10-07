"""C / Linux-kernel intervention backend.

Generic counterparts of the JVM intervention tools: observe suspect function
arguments with a dynamic kprobe, or replace the function body, rebuild when a
behavioral intervention is requested, and re-run the syzkaller reproducer under
QEMU inside the CoHiker docker container.
"""

from src.intervention.c_kernel.api import apply_c_intervention, apply_c_observation

__all__ = ["apply_c_intervention", "apply_c_observation"]
