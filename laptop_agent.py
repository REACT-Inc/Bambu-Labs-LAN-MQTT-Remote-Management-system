"""Opt-in laptop agent. Execute only locally configured argv lists; never shell text."""
import argparse
import json
import logging
import os
import platform
import shutil
import subprocess
import time
from pathlib import Path
import urllib.request


def api(config,path,data):
    req=urllib.request.Request(config['server'].rstrip('/')+'/agent/'+path,
        data=json.dumps(data).encode(),headers={'Content-Type':'application/json','X-PM':'1',
        'X-Device-ID':config['device_id'],'Authorization':'Bearer '+config['token']},method='POST')
    with urllib.request.urlopen(req,timeout=20) as result:return json.load(result)


def write_json(path,data):
    tmp=path.with_name(path.name+'.tmp')
    with open(tmp,'w') as stream:
        json.dump(data,stream);stream.flush();os.fsync(stream.fileno())
    os.replace(tmp,path)


def run_command(config,task):
    commands=config.get('commands',{})
    argv=commands.get(task['action'])
    if not isinstance(argv,list) or not argv or any(not isinstance(x,str) for x in argv):
        return dict(id=task['id'],ok=False,output='Command is not configured on this laptop.')
    try:
        # No shell interpolation; server cannot supply additional command arguments.
        output=subprocess.run(argv,shell=False,capture_output=True,text=True,timeout=45)
        return dict(id=task['id'],ok=output.returncode==0,output=(output.stdout+'\n'+output.stderr)[-4000:])
    except Exception as exc:return dict(id=task['id'],ok=False,output=type(exc).__name__+': '+str(exc))


def main():
    p=argparse.ArgumentParser();p.add_argument('--config',default='agent.json');p.add_argument('--init',action='store_true')
    args=p.parse_args();path=Path(args.config).resolve()
    if args.init:
        if path.exists():raise SystemExit('Configuration already exists; edit it directly.')
        commands={'hostname':['hostname']}
        if os.name=='nt':commands['network']=['ipconfig','/all']
        else:commands['uptime']=['uptime']
        data={'server':input('Management URL (http://100.x.x.x:8080): ').strip(),
              'device_id':input('Device ID from dashboard: ').strip(),'token':input('Enrollment token: ').strip(),'commands':commands}
        write_json(path,data)
        if os.name!='nt':os.chmod(path,0o600)
        print('Configuration saved. Only locally listed commands can execute.');return
    config=json.loads(path.read_text());logging.basicConfig(level=logging.INFO)
    state_file=path.with_suffix('.state.json')
    state=json.loads(state_file.read_text()) if state_file.exists() else {'seen':[],'pending_result':None}
    while True:
        try:
            if state.get('pending_result'):
                api(config,'result',state['pending_result']);state['pending_result']=None;write_json(state_file,state)
            report={'hostname':platform.node(),'platform':platform.platform(),'disk_free':shutil.disk_usage(path.parent).free,'commands':list(config.get('commands',{}))}
            try:
                import psutil
                battery=psutil.sensors_battery()
                report.update(cpu_percent=psutil.cpu_percent(),memory_percent=psutil.virtual_memory().percent,battery=battery.percent if battery else None)
            except ImportError:pass
            reply=api(config,'heartbeat',report);task=reply.get('task')
            if task:
                if task['id'] in state['seen']:
                    result=dict(id=task['id'],ok=False,output='Already seen; execution was not repeated.')
                else:
                    # Persist before executing. A crash/reboot cannot replay the command.
                    state['seen']=(state['seen']+[task['id']])[-1000:];write_json(state_file,state)
                    result=run_command(config,task)
                state['pending_result']=result;write_json(state_file,state)
                api(config,'result',result);state['pending_result']=None;write_json(state_file,state)
        except Exception as exc:logging.warning('Agent connection/task error: %s',type(exc).__name__)
        time.sleep(20)


if __name__=='__main__':main()
