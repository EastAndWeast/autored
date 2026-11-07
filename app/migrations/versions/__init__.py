"""迁移版本注册中心。"""
from .m001_initial_baseline import upgrade as upgrade_001
from .m002_p1_reliable_jobs import upgrade as upgrade_002
from .m003_p4_stats_tracking import upgrade as upgrade_003


def get_all_migrations():
    return [
        (1, "001_initial_baseline", upgrade_001),
        (2, "002_p1_reliable_jobs", upgrade_002),
        (3, "003_p4_stats_tracking", upgrade_003),
    ]

