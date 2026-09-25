"""MeshCentral control. No separate laptop agent and no command retries."""
import asyncio
import base64
import json
import os
import re
import secrets
import ssl
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit,urlunsplit
import aiohttp

DEFAULT_COMMANDS={
    'hostname':{'script':'hostname','type':0},
    'network':{'script':'ipconfig /all','type':0},
    'whoami':{'script':'whoami','type':0},
    'uptime':{'script':'(Get-Date) - (Get-CimInstance Win32_OperatingSystem).LastBootUpTime','type':2},
}


class MeshCentral:
    def __init__(self,core,store):
        self.core,self.store=core,store;self.path=core.DATA_DIR/'meshcentral.json'
        self.config=json.loads(self.path.read_text()) if self.path.exists() else {}
        self.cached=[];self.updated=0;self.attempt=0;self.error='';self.lock=asyncio.Lock();self.tasks=set()
        with store.db:
            store.db.execute("UPDATE device_tasks SET status='unknown',result='Previous agent command retired; never resent through MeshCentral.' WHERE status IN ('pending','delivered')")

    def public(self):
        return {'configured':bool(self.config.get('url') and self.config.get('username') and self.config.get('password')),
            'url':self.config.get('url',''),'username':self.config.get('username',''),'prefix':self.config.get('prefix','REACT-'),
            'error':self.error,'updated':self.updated or None,'commands':list(self.commands())}

    def commands(self):
        result=self.config.get('commands',DEFAULT_COMMANDS)
        return {k:v for k,v in result.items() if re.fullmatch(r'[A-Za-z0-9_-]{1,40}',k) and isinstance(v,dict) and isinstance(v.get('script'),str) and 0<len(v['script'])<=8000 and v.get('type',0) in (0,1,2)}

    def save(self,data):
        if self.tasks:raise ValueError('Wait for active MeshCentral commands before changing the connection.')
        url=str(data.get('url','')).strip().rstrip('/');u=urlsplit(url)
        if u.scheme!='https' or not u.hostname or u.username or u.password or u.fragment or u.query:
            raise ValueError('Use the HTTPS MeshCentral base URL, including any domain path, without credentials or query parameters.')
        username=str(data.get('username','')).strip();password=str(data.get('password',''))
        if not password:
            if url!=self.config.get('url') or username!=self.config.get('username'):
                raise ValueError('Enter a new login-token password when changing server or username.')
            password=self.config.get('password','')
        if not username or not password or max(len(username),len(password))>2048:raise ValueError('Enter the MeshCentral login-token username and password.')
        prefix=str(data.get('prefix','REACT-')).strip()
        if not prefix or len(prefix)>80:raise ValueError('Enter a device-name prefix, such as REACT-.')
        cfg=dict(self.config,url=url,username=username,password=password,prefix=prefix)
        fd,path=tempfile.mkstemp(dir=self.path.parent)
        try:
            with os.fdopen(fd,'w') as f:json.dump(cfg,f);f.flush();os.fsync(f.fileno())
            os.replace(path,self.path)
        finally:
            if os.path.exists(path):os.unlink(path)
        self.config=cfg;self.cached=[];self.attempt=0;self.updated=0;self.error=''

    async def request(self,action,payload=None,sent=None):
        if not self.public()['configured']:raise ValueError('Configure MeshCentral in the dashboard Laptops tab first.')
        cfg=dict(self.config);u=urlsplit(cfg['url']);path=u.path.rstrip('/')+'/control.ashx'
        url=urlunsplit(('wss',u.netloc,path,'',''))
        auth=','.join(base64.b64encode(cfg[k].encode()).decode() for k in ('username','password'))
        rid=secrets.token_hex(16)
        try:
            context=ssl.create_default_context(cafile=cfg.get('ca_file') or None)
            async with asyncio.timeout(45 if action=='runcommands' else 8):
                async with aiohttp.ClientSession() as session:
                    async with session.ws_connect(url,headers={'x-meshauth':auth},ssl=context,max_msg_size=4*1024*1024,heartbeat=15) as ws:
                        if sent:sent()  # From here, failures are uncertain; never retry.
                        await ws.send_json(dict(payload or {},action=action,responseid=rid))
                        async for msg in ws:
                            if msg.type!=aiohttp.WSMsgType.TEXT:continue
                            data=json.loads(msg.data)
                            if data.get('action')=='close':raise ValueError('MeshCentral rejected authentication or access.')
                            if data.get('action')=='ping':await ws.send_json({'action':'pong'});continue
                            if data.get('responseid')==rid and (data.get('action')==action or action=='runcommands' and data.get('action')=='msg' and data.get('type')=='runcommands'):return data
                raise ValueError('MeshCentral closed the connection without confirming the request.')
        except (aiohttp.ClientError,asyncio.TimeoutError,OSError) as exc:
            raise ValueError('MeshCentral connection failed or timed out. Check URL, login token and TLS certificate; requests are not automatically retried.') from None

    def parse_nodes(self,data):
        if data.get('result') and str(data['result']).lower()!='ok':raise ValueError('MeshCentral denied device-list access.')
        groups=data.get('nodes')
        if not isinstance(groups,dict):raise ValueError('MeshCentral returned an unexpected device list.')
        result=[];prefix=self.config.get('prefix','REACT-').casefold();allowed=self.config.get('mesh_ids',[])
        for mesh,items in groups.items():
            if allowed and mesh not in allowed:continue
            if not isinstance(items,list):continue
            for node in items:
                if not isinstance(node,dict):continue
                name=str(node.get('name',''));nid=node.get('_id','')
                if not name.casefold().startswith(prefix) or not nid.startswith('node/'):continue
                windows=(node.get('agent') or {}).get('id') in (1,2,3,4,43) or 'windows' in str(node.get('osdesc','')).lower()
                result.append({'id':nid,'name':name,'online':bool(int(node.get('conn',0))&1),
                    'report':{'hostname':node.get('host') or name,'platform':node.get('osdesc',''),
                    'commands':list(self.commands()) if windows else []}})
        return sorted(result,key=lambda n:n['name'].casefold())

    async def devices(self,force=False):
        async with self.lock:
            if not force and time.time()-self.attempt<20:return self.cached
            self.attempt=time.time()
            if not self.public()['configured']:self.cached=[];return []
            try:
                cfg=self.config
                data=await self.request('nodes')
                if self.config is not cfg:raise ValueError('MeshCentral settings changed during refresh. Refresh again.')
                self.cached=self.parse_nodes(data);self.updated=time.time();self.error=''
            except ValueError as exc:
                self.error=str(exc);self.cached=[]
                if force:raise
            return self.cached

    async def command(self,device,action):
        cfg=self.config
        definition=self.commands().get(action)
        if not definition:raise ValueError('Choose a configured MeshCentral command.')
        nodes=await self.devices(force=True)
        if getattr(self.core,'update_pending',lambda:False)():raise ValueError('Management update in progress.')
        if self.config is not cfg:raise ValueError('MeshCentral settings changed. Review and try again.')
        node=next((d for d in nodes if d['id']==device),None)
        if not node or not node['online'] or action not in node['report']['commands']:
            raise ValueError('Choose an online, permitted Windows laptop from MeshCentral.')
        task=secrets.token_hex(8)
        with self.store.db:self.store.db.execute('INSERT INTO device_tasks(id,device,action,status,created) VALUES(?,?,?,?,?)',(task,device,action,'delivered',time.time()))
        worker=asyncio.create_task(self.run(task,device,action,dict(definition)));self.tasks.add(worker);worker.add_done_callback(self.tasks.discard)
        return task

    async def run(self,task,device,action,definition):
        sent=False
        def sending():
            nonlocal sent
            sent=True
        try:
            data=await self.request('runcommands',{'nodeids':[device],'type':definition.get('type',0),'cmds':definition['script'],'runAsUser':0,'reply':True},sending)
            output=str(data.get('result','No result supplied.'))[:4000]
            if output in ('Access denied','Invalid nodeid','Agent not connected'):status='failed'
            elif output.upper()=='OK':status='submitted';output='MeshCentral accepted the command. Execution/output was not confirmed; check its device console.'
            else:status='response'  # Output received, not proof of a zero exit status.
        except asyncio.CancelledError:
            status='unknown' if sent else 'cancelled';output='Service stopped. Request will not be resent.'
        except Exception:
            status='unknown' if sent else 'failed';output='MeshCentral did not confirm the result. Check its console before retrying.'
        with self.store.db:self.store.db.execute('UPDATE device_tasks SET status=?,result=? WHERE id=?',(status,output,task))
        self.store.event('MeshCentral','Laptop command',f'{device} • {action} • {status} • {task}')

    async def close(self,app):
        for task in list(self.tasks):task.cancel()
        await asyncio.gather(*self.tasks,return_exceptions=True)
