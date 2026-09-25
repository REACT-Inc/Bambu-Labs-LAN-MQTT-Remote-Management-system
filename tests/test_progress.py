import unittest
from progress_notifications import ProgressTracker


class ProgressTests(unittest.TestCase):
    def setUp(self):
        self.t=ProgressTracker()
    def report(self,percent,state='RUNNING',name='A',file='part.3mf'):
        return self.t.update(name,dict(gcode_state=state,mc_percent=percent,subtask_name=file))
    def test_milestones_once(self):
        self.report(0,'IDLE')
        emitted=[self.report(x) for x in (0,9,10,10,11,19,20,20,30,90,100)]
        self.assertEqual([x for x in emitted if x is not None],[10,20,30,90])
    def test_start_mid_print_no_backlog(self):
        self.assertIsNone(self.report(45))
        self.assertIsNone(self.report(49))
        self.assertEqual(self.report(50),50)
    def test_reconnect_and_regression_no_duplicates(self):
        self.report(0,'IDLE');self.report(20)
        self.report(20,'UNKNOWN')
        self.assertIsNone(self.report(20))
        self.assertIsNone(self.report(10))
        self.assertEqual(self.report(30),30)
    def test_pause_resume(self):
        self.report(0);self.report(10)
        self.assertIsNone(self.report(20,'PAUSE'))
        self.assertEqual(self.report(20),20)
    def test_reprint_same_file(self):
        self.report(0);self.report(90);self.report(100,'FINISH')
        self.assertEqual(self.report(10),10)
    def test_skipped_percent_reports_latest_only(self):
        self.report(0)
        self.assertEqual(self.report(36),30)
        self.assertIsNone(self.report(39))
    def test_printers_independent_and_bad_values(self):
        self.report(0,name='A');self.report(0,name='B')
        self.assertEqual(self.report(10,name='A'),10)
        self.assertEqual(self.report(10,name='B'),10)
        for value in ('bad',None,-1,101,float('nan')):
            self.assertIsNone(self.report(value))
