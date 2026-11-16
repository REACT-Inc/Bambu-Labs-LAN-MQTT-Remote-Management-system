"""Following the Pi's Tailscale address after it changes (tailscale_address.py, pm-doctor's serve_missing)."""
import asyncio,subprocess,sys,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import tailscale_address as ta

ROOT=Path(__file__).parents[1]
sys.path.insert(0,str(ROOT/'doctor'))
import pm_doctor as doc


class AddressTests(unittest.TestCase):
    def test_range(self):
        for a in ('100.64.0.1','100.101.102.103','100.127.255.254'):self.assertTrue(ta.is_tailscale(a),a)
        for a in ('100.63.255.255','100.128.0.1','192.168.1.50','127.0.0.1','tailscale',''):self.assertFalse(ta.is_tailscale(a),a)

    def test_read_from_the_interface_or_the_command(self):
        run=MagicMock(side_effect=AssertionError('not needed'))
        self.assertEqual(ta.tailscale_ipv4(run=run,interface=lambda n:'100.101.1.2',names=['tailscale0']),'100.101.1.2')
        def gone(name):raise OSError('No such device')
        run=lambda *a,**k:SimpleNamespace(stdout='100.88.7.6\n')
        self.assertEqual(ta.tailscale_ipv4(run=run,interface=gone,names=[]),'100.88.7.6')
        self.assertEqual(ta.tailscale_ipv4(run=lambda *a,**k:SimpleNamespace(stdout='fd7a:115c:a1e0::1\n'),interface=lambda n:'10.0.0.5',names=['tailscale0']),None)
        def missing(*a,**k):raise FileNotFoundError('tailscale')
        self.assertIsNone(ta.tailscale_ipv4(run=missing,interface=gone,names=[]))
        def slow(*a,**k):raise subprocess.TimeoutExpired('tailscale',5)
        self.assertIsNone(ta.tailscale_ipv4(run=slow,interface=gone,names=[]))


class Site:
    def __init__(self,host):self.host,self.stopped=host,False
    async def stop(self):self.stopped=True


class ListenerTests(unittest.IsolatedAsyncioTestCase):
    def listener(self,present,tailscale=None,hosts=('127.0.0.1','100.101.1.1')):
        self.present,self.now,self.events=set(present),tailscale,[]
        async def open_site(host,port):
            if host not in self.present:raise OSError(99,'Cannot assign requested address')
            return Site(host)
        return ta.Listener(open_site,list(hosts),8080,MagicMock(),tailscale=lambda:self.now,event=lambda *a:self.events.append(a),every=0)

    async def test_a_changed_tailscale_address_is_followed(self):
        l=self.listener({'127.0.0.1','100.102.2.2'},'100.102.2.2')   # signed in again: a new address
        await l.start()
        self.assertEqual(set(l.sites),{'127.0.0.1','100.102.2.2'});self.assertEqual(l.followed,'100.102.2.2')
        self.assertEqual(self.events[0][1],'Dashboard address changed');self.assertIn('http://100.102.2.2:8080',self.events[0][2])
        self.assertEqual(l.status()[0]['key'],'listen-tailscale');self.assertIn('100.101.1.1',l.status()[0]['detail'])
        # it changes again
        self.present.add('100.103.3.3');self.now='100.103.3.3';old=l.sites['100.102.2.2']
        await l.follow()
        self.assertTrue(old.stopped);self.assertEqual(l.followed,'100.103.3.3');self.assertNotIn('100.102.2.2',l.sites)
        # the configured address comes back: back to it
        self.present.add('100.101.1.1');self.now='100.101.1.1'
        task=asyncio.create_task(l.watch());await asyncio.sleep(0.05);task.cancel()
        self.assertIn('100.101.1.1',l.sites);self.assertIsNone(l.followed);self.assertEqual(l.missing,[]);self.assertEqual(l.status(),[])

    async def test_without_tailscale_it_keeps_trying(self):
        l=self.listener({'127.0.0.1'},None)
        await l.start()
        self.assertEqual(l.missing,['100.101.1.1']);self.assertIsNone(l.followed);self.assertEqual(self.events,[])
        self.assertEqual(l.status()[0]['level'],'warn');self.assertIn('tailscale status',l.status()[0]['detail'])
        errors=l.log.error.call_count
        task=asyncio.create_task(l.watch());await asyncio.sleep(0.05);task.cancel()
        self.assertEqual(l.log.error.call_count,errors)   # (retries don't fill the log)
        self.present.add('100.101.1.1')   # Tailscale connects later with the same address
        task=asyncio.create_task(l.watch());await asyncio.sleep(0.05);task.cancel()
        self.assertIn('100.101.1.1',l.sites)

    async def test_other_addresses_are_retried_and_never_replaced(self):
        l=self.listener({'127.0.0.1'},'100.102.2.2',hosts=('127.0.0.1','192.168.4.1'))   # a hotspot not up yet
        await l.start()
        self.assertEqual(l.missing,['192.168.4.1']);self.assertIsNone(l.followed)   # (not a Tailscale address: not swapped)
        self.assertIn('config.json',l.status()[0]['detail'])

    async def test_nothing_opens(self):
        l=self.listener(set(),None)
        with self.assertRaises(RuntimeError):await l.start()

    async def test_real_sockets(self):
        from aiohttp import web
        runner=web.AppRunner(web.Application());await runner.setup();self.addAsyncCleanup(runner.cleanup)
        async def open_site(host,port):
            site=web.TCPSite(runner,host,port);await site.start();return site
        l=ta.Listener(open_site,['127.0.0.1','100.64.0.1'],0,MagicMock(),tailscale=lambda:'127.0.0.2')   # 100.64.0.1 isn't on this computer
        await l.start()
        self.assertEqual(set(l.sites),{'127.0.0.1','127.0.0.2'});self.assertEqual(l.followed,'127.0.0.2')


class DoctorTests(unittest.TestCase):
    def test_the_status_page_follows_tailscale_too(self):
        self.assertEqual(doc.DOCTOR_VERSION,'1.0.1')   # (install-doctor.sh replaces the installed copy only when this changes)
        self.assertEqual(doc.tailscale_ipv4(run=lambda *a,**k:SimpleNamespace(stdout='100.90.1.2\n')),'100.90.1.2')
        servers=doc.serve(None,['127.0.0.1'],0)
        try:
            port=servers[0].server_address[1]
            new=doc.serve_missing(None,['127.0.0.1','100.64.0.1'],servers,port,tailscale=lambda:'127.0.0.2')
            self.assertEqual([s.server_address[0] for s in new],['127.0.0.2'])
            servers+=new
            self.assertEqual(doc.serve_missing(None,['127.0.0.1','100.64.0.1'],servers,port,tailscale=lambda:'127.0.0.2'),[])   # already there
        finally:
            for s in servers:s.shutdown();s.server_close()


if __name__=='__main__':
    unittest.main()
