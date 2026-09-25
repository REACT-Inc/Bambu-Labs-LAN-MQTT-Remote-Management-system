"""Optional Windows service wrapper. Requires pywin32; install from an admin shell."""
import os
from pathlib import Path
import subprocess
import sys
import win32event
import win32service
import win32serviceutil

class AgentService(win32serviceutil.ServiceFramework):
    _svc_name_='PrinterManagementAgent'
    _svc_display_name_='3D Printer Management Laptop Agent'
    _svc_description_='Reports laptop status and runs locally approved management commands.'
    def __init__(self,args):
        super().__init__(args);self.stop_event=win32event.CreateEvent(None,0,0,None);self.process=None
    def SvcStop(self):
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        win32event.SetEvent(self.stop_event)
        if self.process:self.process.terminate()
    def SvcDoRun(self):
        root=Path(__file__).resolve().parent
        python=Path(sys.exec_prefix)/'python.exe'
        with open(root/'agent-service.log','a',encoding='utf-8') as log:
            while win32event.WaitForSingleObject(self.stop_event,0)!=win32event.WAIT_OBJECT_0:
                self.process=subprocess.Popen([str(python),str(root/'laptop_agent.py'),'--config',str(root/'agent.json')],cwd=root,stdout=log,stderr=subprocess.STDOUT)
                while self.process.poll()is None:
                    if win32event.WaitForSingleObject(self.stop_event,1000)==win32event.WAIT_OBJECT_0:
                        self.process.terminate();return
                if win32event.WaitForSingleObject(self.stop_event,10000)==win32event.WAIT_OBJECT_0:return

if __name__=='__main__':win32serviceutil.HandleCommandLine(AgentService)
