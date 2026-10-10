"""The status icon at the top left: status (worst first) and the notifications sent."""
import sys,unittest
from pathlib import Path

sys.path.insert(0,str(Path(__file__).parents[1]))
from status_center import StatusCenter


class StatusTests(unittest.TestCase):
    def test_worst_level_and_order(self):
        clock=iter(range(100)).__next__;center=StatusCenter({1:'error',2:'ok'},clock=clock)
        self.assertEqual(center.snapshot()['level'],'ok')
        center.set('bed-ai','Downloading the bed check AI',level='busy',progress=1.7)
        center.add_source(lambda:[dict(key='version',title='Version 1.7.6',level='ok'),dict(key='p',title='A1 reports an error',level='warn')])
        snap=center.snapshot()
        self.assertEqual(snap['level'],'warn');self.assertEqual([i['key'] for i in snap['items']],['p','bed-ai','version'])
        self.assertEqual(snap['items'][1]['progress'],1.0)   # kept within 0-1
        center.clear('bed-ai');self.assertNotIn('bed-ai',[i['key'] for i in center.snapshot()['items']])
        with self.assertRaises(ValueError):center.set('x','y',level='purple')

    def test_a_broken_source_does_not_break_the_icon(self):
        center=StatusCenter()
        center.add_source(lambda:1/0);center.add_source(lambda:[dict(key='ok',title='fine',level='ok')])
        with self.assertLogs('status_center','ERROR'):self.assertEqual(len(center.snapshot()['items']),1)

    def test_notifications_newest_first_and_capped(self):
        center=StatusCenter({0xE74C3C:'error'})
        for n in range(60):center.note('A1',f'Alert {n}','details',0xE74C3C if n==59 else None)
        snap=center.snapshot()
        self.assertEqual(len(snap['notifications']),50);self.assertEqual(snap['latest'],60)
        newest=snap['notifications'][0];self.assertEqual((newest['title'],newest['level'],newest['id']),('Alert 59','error',60))
        self.assertEqual(snap['notifications'][1]['level'],'info')


    def test_progress_keeps_only_the_latest_per_printer_and_text_is_plain(self):
        center=StatusCenter()
        for percent in (10,20,30):center.note('A1',f'🖨️ Print progress • {percent}%','**File:** part.3mf 🟦🟦⬜⬜ **30%**')
        center.note('H2D','🖨️ Print progress • 50%','x');center.note('A1','✅ Print complete','**Done**')
        titles=[n['title'] for n in center.snapshot()['notifications']]
        self.assertEqual(titles,['✅ Print complete','🖨️ Print progress • 50%','🖨️ Print progress • 30%'])
        self.assertEqual(center.snapshot()['notifications'][2]['detail'],'File: part.3mf 30%')

if __name__=='__main__':
    unittest.main()
