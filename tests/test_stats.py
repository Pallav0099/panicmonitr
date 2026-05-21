import unittest

from src.stats import ContainerInfo, ProcessInfo, StatsCollector, SystemSnapshot


class _FakeImage:
    tags = ["example:latest"]
    id = "imageabcdef123456"


class _FakeContainer:
    id = "abcdef1234567890"
    name = "/api"
    image = _FakeImage()
    status = "exited"
    attrs = {
        "State": {
            "RestartCount": 7,
            "StartedAt": "0001-01-01T00:00:00Z",
        },
        "HostConfig": {"RestartCount": 99},
        "Config": {},
        "NetworkSettings": {},
    }


class StatsTests(unittest.TestCase):
    def test_system_snapshot_to_dict_uses_asdict_without_extra_list_copies(self) -> None:
        snap = SystemSnapshot(
            timestamp="2026-05-22T10:00:00+05:30",
            hostname="host",
            cpu_percent=1.0,
            cpu_count=4,
            load_avg_1m=0.1,
            load_avg_5m=0.2,
            load_avg_15m=0.3,
            mem_total_bytes=100,
            mem_used_bytes=50,
            mem_available_bytes=50,
            mem_percent=50.0,
            swap_total_bytes=0,
            swap_used_bytes=0,
            swap_percent=0.0,
            disk_total_bytes=100,
            disk_used_bytes=40,
            disk_free_bytes=60,
            disk_percent=40.0,
            disk_read_bytes=1,
            disk_write_bytes=2,
            net_sent_bytes=3,
            net_recv_bytes=4,
            cpu_temp=None,
            process_count=1,
            containers=[
                ContainerInfo(
                    id="abc",
                    name="api",
                    image="example:latest",
                    status="running",
                    health=None,
                    cpu_percent=0.0,
                    mem_usage_bytes=0,
                    mem_limit_bytes=0,
                    net_rx_bytes=0,
                    net_tx_bytes=0,
                    uptime_seconds=1,
                    restart_count=2,
                )
            ],
            processes=[
                ProcessInfo(
                    pid=1,
                    ppid=0,
                    name="init",
                    username="root",
                    status="sleeping",
                    cpu_percent=0.0,
                    mem_percent=0.0,
                    mem_rss_bytes=0,
                    threads=1,
                    nice=0,
                    cmdline="init",
                )
            ],
        )

        data = snap.to_dict()
        self.assertEqual(data["containers"][0]["restart_count"], 2)
        self.assertEqual(data["processes"][0]["pid"], 1)

    def test_container_restart_count_comes_from_state(self) -> None:
        collector = StatsCollector(include_docker=False)
        info = collector._collect_one_container(_FakeContainer())
        self.assertEqual(info.restart_count, 7)

