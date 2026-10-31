import json
import os
import tempfile
import unittest
from unittest import mock

import config
import stats


class StatsTest(unittest.TestCase):
    def test_parse_cpu_and_meminfo(self):
        self.assertEqual(stats.parse_cpu("cpu  10 0 10 70 10 0 0 0 0 0\ncpu0 1 2 3\n"), (20, 100))
        self.assertEqual(stats.parse_meminfo("MemTotal:  1000 kB\nMemFree: 1 kB\nMemAvailable:  400 kB\n"), (1024000, 409600))

    def test_sampler_deltas(self):
        with tempfile.TemporaryDirectory() as tmp:
            for d in ("proc", "cg/system.slice/box@vma.service", "data/boxdefs", "data/boxes/vma"):
                os.makedirs(os.path.join(tmp, d))
            open(os.path.join(tmp, "data/boxdefs/vma.json"), "w").close()
            open(os.path.join(tmp, "data/boxdefs/vmb.json"), "w").close()  # stopped box
            meminfo = os.path.join(tmp, "proc/meminfo")
            open(meminfo, "w").write("MemTotal: 1000 kB\nMemAvailable: 250 kB\n")
            cg = os.path.join(tmp, "cg/system.slice/box@vma.service")
            open(os.path.join(cg, "memory.current"), "w").write("4096\n")
            config.CFG["data_root"] = os.path.join(tmp, "data")

            def put(stat_line, usec):
                open(os.path.join(tmp, "proc/stat"), "w").write(stat_line)
                open(os.path.join(cg, "cpu.stat"), "w").write("usage_usec %d\nuser_usec 1\n" % usec)

            s = stats.Sampler()
            with mock.patch.object(stats, "PROC", os.path.join(tmp, "proc")), \
                 mock.patch.object(stats, "CGROUP", os.path.join(tmp, "cg")), \
                 mock.patch("units.is_active", side_effect=lambda u: u == "box@vma.service"), \
                 mock.patch.object(stats, "cgroup_dir", return_value=cg):
                put("cpu 0 0 0 100 0 0 0 0\n", 0)
                s.sample(now=100.0)
                put("cpu 25 0 0 175 0 0 0 0\n", 2_500_000)   # 25 of 100 jiffies busy; 2.5 s CPU in 5 s
                s.sample(now=105.0)
            self.assertEqual(stats._data["host"]["cpu_percent"], 25.0)
            self.assertEqual(stats._data["host"]["ram_used"], 750 * 1024)
            self.assertEqual(stats._data["vms"]["vma"]["cpu_percent"], 50.0)
            self.assertEqual(stats._data["vms"]["vma"]["ram"], 4096)
            self.assertFalse(stats._data["vms"]["vmb"]["active"])
            self.assertNotIn("ram", stats._data["vms"]["vmb"])

    def test_console_user_sees_only_own_vms(self):
        stats._data["host"], stats._data["vms"] = {}, {"vma": {}, "vmb": {}}
        with mock.patch.object(stats, "start"):
            self.assertEqual(list(stats.get({"role": "console", "vms": ["vmb"]})["vms"]), ["vmb"])
            self.assertEqual(sorted(stats.get({"role": "admin", "vms": []})["vms"]), ["vma", "vmb"])


if __name__ == "__main__":
    unittest.main()
