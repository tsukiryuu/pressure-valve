import os, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pressure_valve as pv

CFG = dict(pv.DEFAULTS)
R = lambda **k: pv.Reading(**{"pressure_pct": 60, "committed_pct": 60, "free_pct": 40, "kernel_level": 1,
                              "swap_mb": 0, "disk_free_gb": 100, **k})

class Policy(unittest.TestCase):
    def test_calm(self):
        self.assertEqual(pv.assess(R(), CFG)[0], "ok")
    def test_sticky_swap_alone_is_not_an_emergency(self):
        # days of accumulated swap, plenty of disk, kernel calm: warn, never critical
        self.assertEqual(pv.assess(R(swap_mb=9000, disk_free_gb=60), CFG)[0], "warn")
    def test_deep_swap_moderate_disk_needs_kernel_confirmation(self):
        self.assertEqual(pv.assess(R(swap_mb=9000, disk_free_gb=20, kernel_level=1), CFG)[0], "warn")
        self.assertEqual(pv.assess(R(swap_mb=9000, disk_free_gb=20, kernel_level=2), CFG)[0], "critical")
    def test_hard_floor_is_unconditional(self):
        self.assertEqual(pv.assess(R(swap_mb=9000, disk_free_gb=10, kernel_level=1), CFG)[0], "critical")
    def test_pressure_and_kernel_critical(self):
        self.assertEqual(pv.assess(R(pressure_pct=91), CFG)[0], "critical")
        self.assertEqual(pv.assess(R(kernel_level=4), CFG)[0], "critical")

class Parsers(unittest.TestCase):
    def test_vm_stat(self):
        page, p = pv.parse_vm_stat("Mach Virtual Memory Statistics: (page size of 16384 bytes)\nPages active:   1000.\nPages wired down:  500.\n")
        self.assertEqual((page, p["Pages active"], p["Pages wired down"]), (16384, 1000, 500))
    def test_swap(self):
        self.assertEqual(pv.parse_swap("total = 9216.00M  used = 7834.25M  free = 1381.75M  (encrypted)"), 7834.25)
    def test_top_cmprs(self):
        txt = "PID    CMPRS\n59216* 1200M+\n412    2G\n77     900K\n"
        self.assertEqual(pv.parse_top_cmprs(txt, 900), {59216: 1200, 412: 2048})

class Reflexes(unittest.TestCase):
    def test_matching_never_hits_self_or_init(self):
        procs = [(1, "launchd"), (os.getpid(), "python worker.py"), (4242, "python worker.py --batch"), (5, "python pressure_valve.py watch")]
        self.assertEqual([p for p, _ in pv.matching([r"worker\.py"], procs)], [4242])

if __name__ == "__main__":
    unittest.main()
