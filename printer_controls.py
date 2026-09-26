"""Validated controls shared by Discord and the authenticated dashboard."""
import asyncio
import json
import math
import time
from thermal_controls import fans,fan_commands,model_name

SPEEDS={'silent':1,'standard':2,'sport':3,'ludicrous':4}


def limits(core,name):
    p=getattr(core,'printer_config',lambda n:None)(name) or {}
    model=str(p.get('model') or name).lower().replace(' ','').replace('_','')
    return {'chamber':65 if 'h2d' in model else 0,'fans':fans(core,name), 'nozzle':350 if 'h2d' in model else 300,
            'bed':120 if 'h2d' in model else 80 if 'mini' in model else 100 if 'a1' in model else 80}


def number(value,low,high,integer=False):
    if isinstance(value,bool):raise ValueError('Enter a number.')
    try:n=float(value)
    except (TypeError,ValueError):raise ValueError('Enter a number.')
    if not math.isfinite(n) or not low<=n<=high or (integer and not n.is_integer()):
        raise ValueError(f'Value must be {"a whole number " if integer else ""}between {low} and {high}.')
    return int(n) if integer else n


def prepare(core,name,kind,value,axis=None):
    if name not in core.names():raise ValueError('Unknown printer.')
    if kind in ('nozzle','bed'):
        value=number(value,0,limits(core,name)[kind],True)
        return 'gcode_line',f'{"M104" if kind=="nozzle" else "M140"} S{value}\n',f'{kind.title()} target → {value} °C'
    if kind=='speed':
        if value not in SPEEDS:raise ValueError('Choose silent, standard, sport or ludicrous.')
        return 'print_speed',str(SPEEDS[value]),f'Print speed → {value}'
    if kind=='chamber':
        if 'h2d' not in model_name(core,name):raise ValueError('Active chamber heating is only enabled for H2D printers.')
        value=number(value,0,65,True)
        if value and value<40:raise ValueError('Use 0 to turn chamber heating off, or 40–65 °C.')
        return 'batch',[{'command':'set_ctt','ctt_val':value}],f'Chamber target → {value} °C'
    if kind=='fan' or kind=='fanall' or kind.startswith('fan_'):
        value=number(value,0,100,True)
        target='all' if kind=='fanall' else kind[4:] if kind.startswith('fan_') else 'part'
        commands,label=fan_commands(core,name,target,value)
        return 'batch',commands,label
    if kind=='move':
        if axis not in ('X','Y','Z'):raise ValueError('Choose X, Y or Z.')
        value=number(value,-(1 if axis=='Z' else 10),1 if axis=='Z' else 10)
        if not value or abs(value)<0.1:raise ValueError('Move at least 0.1 mm.')
        return 'gcode_line',f'G91\nG1 {axis}{value:g} F{300 if axis=="Z" else 1200}\nG90\nM400\n',f'Move {axis} by {value:+g} mm'
    raise ValueError('Unknown control.')


class Controls:
    def __init__(self,core,store):
        self.core,self.store,self.moved=core,store,{}
        self.pending={};core.control_response_listener=self.handle_response

    def handle_response(self,name,response):
        key=str(response.get('sequence_id',''));pending=self.pending.get(key)
        if not pending or pending[0]!=name or response.get('command')!=pending[1]:return
        self.pending.pop(key,None)
        code=response.get('errno',response.get('err_code',0))
        rejected=str(response.get('result','')).lower() in ('fail','failed','error') or code not in (None,0,'0')
        if not rejected:return
        reason=str(response.get('reason') or response.get('message') or 'Printer rejected the setting.')[:500]
        if response.get('command')=='set_ctt':
            if str(code)=='-2':reason='Chamber heating rejected because a low-temperature filament is loaded (PLA/PETG/TPU).'
            elif str(code)=='-4':reason='Targets below 40 °C disable chamber heating.'
        detail=f'{pending[2]} • {reason} (code {code})'
        self.store.event(name,'Control rejected by printer',detail)
        if hasattr(self.core,'notify'):
            asyncio.create_task(self.core.notify(name,'Control rejected',detail,self.core.RED))


    def apply(self,name,kind,value,axis=None,confirmed=False,homed=False,author='unknown'):
        if getattr(self.core,'update_pending',lambda:False)():raise ValueError('Management update in progress.')
        command,param,label=prepare(self.core,name,kind,value,axis)
        if confirmed is not True:raise ValueError('Confirm this control change.')
        state,error,data,connected=self.core.state_data(name)
        if not connected:raise ValueError('Printer is offline.')
        if kind=='move':
            if homed is not True:raise ValueError('Confirm the printer was homed and the movement area is clear.')
            if state not in ('IDLE','FINISH') or error:raise ValueError('Jogging requires an idle, error-free printer; paused prints are blocked.')
            if not self.core.EXAMPLE_MODE and time.time()-self.core.last_seen.get(name,0)>15:
                raise ValueError('Telemetry is stale. Wait for a fresh printer report.')
            if any(j['printer']==name and j['status'] in ('staging','awaiting_start','printing','paused','needs_review') for j in self.store.jobs()):
                raise ValueError('Resolve the active queue job before jogging.')
            if time.monotonic()-self.moved.get(name,-100)<3:raise ValueError('Wait three seconds between moves.')
        if self.core.EXAMPLE_MODE:
            d=self.core.EXAMPLE_DATA[name]
            if kind in ('nozzle','bed'):d[kind+'_target_temper']=int(value)
            elif kind=='speed':d['spd_lvl']=SPEEDS[value]
            elif kind=='chamber':d['ctt']=int(value)
            elif kind.startswith('fan'):d['demo_fan_targets']={c.get('fan_index',c.get('param')):int(value) for c in param}
        else:
            client=self.core.clients.get(name)
            if not client or not client.is_connected():raise ValueError('Printer disconnected.')
            commands=param if command=='batch' else [{'command':command,'param':param}]
            submitted=0
            for item in commands:
                self.pending={k:v for k,v in self.pending.items() if time.monotonic()-v[3]<300}
                sequence=str(time.time_ns()%1000000000)
                self.pending[sequence]=(name,item['command'],label,time.monotonic())
                payload={'print':dict(item,sequence_id=sequence)}
                result=client.publish(f"device/{self.core.printer_config(name)['serial']}/request",json.dumps(payload),qos=1)
                if result.rc!=0:raise ValueError(f'MQTT submission failed after {submitted}/{len(commands)} commands. Inspect actual fan/settings state before retrying.')
                submitted+=1
        if kind=='move':self.moved[name]=time.monotonic()
        self.store.event(name,'Demo control' if self.core.EXAMPLE_MODE else 'Control submitted',f'{label} • {author}')
        return label+(' • Demo only.' if self.core.EXAMPLE_MODE else ' • Submitted; verify the result on the printer.')
