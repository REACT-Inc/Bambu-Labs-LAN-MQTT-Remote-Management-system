"""Validated controls shared by Discord and the authenticated dashboard."""
import asyncio
import json
import math
import re
import time
from thermal_controls import fans,fan_commands
import printer_models
import filament_sides

SPEEDS={'silent':1,'standard':2,'sport':3,'ludicrous':4}
# Bambu generic filament presets: tray_info_idx and nozzle temperature range sent with ams_filament_setting.
FILAMENTS={'PLA':('GFL99',190,230),'PETG':('GFG99',220,260),'ABS':('GFB99',240,270),'ASA':('GFB98',240,270),
           'TPU':('GFU99',200,250),'PC':('GFC99',260,290),'PA':('GFN99',260,290),'PVA':('GFS99',190,230)}
NOZZLE_DIAMETERS=(0.2,0.4,0.6,0.8)
NOZZLE_TYPES={'stainless_steel':'Stainless steel','hardened_steel':'Hardened steel','tungsten_carbide':'Tungsten carbide'}
EXTERNAL_SPOOL=255
EXTERNAL_LEFT=254
# Bambu printers keep reporting FINISH or FAILED after a print until the next one starts, while sitting idle and ready.
IDLE_STATES=('IDLE','FINISH','FAILED')


def limits(core,name):
    # Per model from printer_models (#6), with config.json "limits" overrides for nozzle and bed.
    model=printer_models.printer(core,name)
    return {**printer_models.limits(core,name),'fans':fans(core,name),'model':model['name'] if model['key'] else '',
            'camera_type':model['camera'],'tested':model['tested']}


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
    if kind in ('nozzle_left','nozzle_right'):
        # Dual-nozzle printers: Bambu Studio's set_nozzle_temp with extruder_index 0 = right, 1 = left (multi-hotend).
        if not dual_nozzle(core,name):raise ValueError('This printer has a single nozzle; set the Nozzle temperature instead.')
        value=number(value,0,limits(core,name)['nozzle'],True)
        side=filament_sides.RIGHT if kind=='nozzle_right' else filament_sides.LEFT
        return 'batch',[{'command':'set_nozzle_temp','extruder_index':side,'target_temp':value}],f'{filament_sides.SIDES[side]} nozzle target → {value} °C'
    if kind=='speed':
        if value not in SPEEDS:raise ValueError('Choose silent, standard, sport or ludicrous.')
        return 'print_speed',str(SPEEDS[value]),f'Print speed → {value}'
    if kind=='chamber':
        top=printer_models.limits(core,name)['chamber']
        if not top:raise ValueError('Active chamber heating is only available on H2-series printers (H2D, H2S, H2C).')
        value=number(value,0,top,True)
        if value and value<40:raise ValueError(f'Use 0 to turn chamber heating off, or 40–{top} °C.')
        return 'batch',[{'command':'set_ctt','ctt_val':value}],f'Chamber target → {value} °C'
    if kind=='fan' or kind=='fanall' or kind.startswith('fan_'):
        value=number(value,0,100,True)
        target='all' if kind=='fanall' else kind[4:] if kind.startswith('fan_') else 'part'
        commands,label=fan_commands(core,name,target,value)
        return 'batch',commands,label
    if kind=='move':
        if axis not in ('X','Y','Z'):raise ValueError('Choose X, Y or Z.')
        value=number(value,-(1 if axis=='Z' else 10),1 if axis=='Z' else 10)
        value=round(value,1)
        if abs(value)<0.1:raise ValueError('Move at least 0.1 mm.')
        # Exactly what Bambu Studio sends (DevAxis::Ctrl_Axis): soft endstops on, relative mode saved and restored,
        # F3000 on X/Y and F900 on Z (StatusPanel::on_axis_ctrl_*).
        gcode=(f'M211 S \nM211 X1 Y1 Z1\nM1002 push_ref_mode\nG91 \nG1 {axis}{value:.1f} F{900 if axis=="Z" else 3000}\n'
               'M1002 pop_ref_mode\nM211 R\n')
        return 'gcode_line',gcode,f'Move {axis} by {value:+g} mm'
    if kind=='home':
        return 'gcode_line','G28 \n','Home all axes'
    if kind=='filament':
        # Same fields as Bambu Studio's "Edit filament" (command ams_filament_setting). External spool: ams_id 255, tray 254.
        if not isinstance(value,dict):raise ValueError('Choose a slot, material and colour.')
        material=str(value.get('type','')).upper()
        if material not in FILAMENTS:raise ValueError('Material must be one of: '+', '.join(FILAMENTS)+'.')
        color=str(value.get('color','')).lstrip('#').upper()
        if not re.fullmatch(r'[0-9A-F]{6}',color):raise ValueError('Colour must be a hex colour like #FF8800.')
        ams=value.get('ams')
        if ams in ('external',EXTERNAL_SPOOL,str(EXTERNAL_SPOOL)):ams_id,slot,tray_id,where=EXTERNAL_SPOOL,0,254,'External spool'
        # Left external spool of a dual-nozzle printer (H2D); Bambu Studio sends ams_id 254, tray_id 254.
        elif ams in ('external_left',EXTERNAL_LEFT,str(EXTERNAL_LEFT)):ams_id,slot,tray_id,where=EXTERNAL_LEFT,0,254,'Left external spool'
        else:
            ams_id=number(ams,0,7,True);slot=number(value.get('slot'),0,3,True);tray_id=slot
            where=f'AMS {ams_id+1} slot {slot+1}'
        idx,low,high=FILAMENTS[material]
        return 'batch',[{'command':'ams_filament_setting','ams_id':ams_id,'tray_id':tray_id,'slot_id':slot,'tray_info_idx':idx,
            'setting_id':'','tray_color':color+'FF','tray_type':material,'nozzle_temp_min':low,'nozzle_temp_max':high}],f'{where} → {material} #{color}'
    if kind=='nozzle_size':
        # Bambu Studio's printer-parts setting: {"system": {"command": "set_accessories", "accessory_type": "nozzle", ...}}.
        if not isinstance(value,dict):raise ValueError('Choose a nozzle diameter and type.')
        diameter=number(value.get('diameter'),0.2,0.8)
        if diameter not in NOZZLE_DIAMETERS:raise ValueError('Nozzle diameter must be 0.2, 0.4, 0.6 or 0.8 mm.')
        nozzle=str(value.get('type',''))
        if nozzle not in NOZZLE_TYPES:raise ValueError('Nozzle type must be stainless steel, hardened steel or tungsten carbide.')
        return 'batch',[{'_root':'system','command':'set_accessories','accessory_type':'nozzle','nozzle_diameter':diameter,'nozzle_type':nozzle}],\
            f'Nozzle → {diameter:g} mm {NOZZLE_TYPES[nozzle].lower()}'
    raise ValueError('Unknown control.')


def dual_nozzle(core,name):
    """True for printers with two nozzles: known from the model, or reported by the printer (device.extruder)."""
    if printer_models.printer(core,name)['dual_nozzle']:return True
    try:return filament_sides.is_dual(core.state_data(name)[2])
    except Exception:return False


def mqtt_homing_supported(data):
    # Bambu Studio (MachineObject::command_go_home) homes printers with bit 32 of the "fun" flags set via the
    # back_to_center command, and others with G28.
    try:return bool(int(str(data.get('fun') or '0'),16)>>32&1)
    except ValueError:return False


def axis_ctrl_supported(data):
    # Bambu Studio (DevAxis::Ctrl_Axis) jogs printers with bit 38 of the hex "fun" flags set via xyz_ctrl, not G-code.
    try:return bool(int(str(data.get('fun') or '0'),16)>>38&1)
    except ValueError:return False


def unhomed_axes(data):
    """Axes the printer reports as not homed (bits 0-2 of home_flag are X, Y, Z; 0 or missing means unknown).

    The firmware ignores jogs on an axis that isn't homed, for example after the motors were released while idle,
    so Bambu Studio (StatusPanel::on_axis_ctrl_xy, DevAxis::IsAxisAtHome*) asks to home first instead of sending them.
    """
    try:flag=int(data.get('home_flag') or 0)
    except (TypeError,ValueError):return []
    if flag==0:return []
    return [axis for bit,axis in enumerate('XYZ') if not flag>>bit&1]


def xyz_ctrl(axis,value):
    step=abs(value)
    if step not in ((1,) if axis=='Z' else (1,10)):
        raise ValueError('This printer only jogs in fixed steps: ±1 mm on Z, ±1 or ±10 mm on X/Y.')
    return [{'command':'xyz_ctrl','axis':axis,'dir':1 if value>0 else -1,'mode':1 if step==10 else 0}]


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
        if kind in ('move','home'):
            if kind=='move' and homed is not True:raise ValueError('Confirm the printer was homed and the movement area is clear.')
            if state not in IDLE_STATES or error:raise ValueError(('Homing' if kind=='home' else 'Jogging')+' requires an idle, error-free printer; paused prints are blocked.')
            if not self.core.EXAMPLE_MODE and time.time()-self.core.last_seen.get(name,0)>15:
                raise ValueError('Telemetry is stale. Wait for a fresh printer report.')
            # A job left "needs review" no longer blocks moves: the printer itself reports it is idle (checked above).
            if any(j['printer']==name and j['status'] in ('staging','awaiting_start','printing','paused') for j in self.store.jobs()):
                raise ValueError('Resolve the active queue job before '+('homing.' if kind=='home' else 'jogging.'))
            if time.monotonic()-self.moved.get(name,-100)<3:raise ValueError('Wait three seconds between moves.')
            if kind=='move' and axis in unhomed_axes(data):
                raise ValueError(f'The printer reports that {axis} is not homed, so it would ignore the move. Press Home (or /home) first, then try again.')
            if kind=='move' and axis_ctrl_supported(data):command,param=('batch',xyz_ctrl(axis,round(float(value),1)))
            if kind=='home' and mqtt_homing_supported(data):command,param=('batch',[{'command':'back_to_center'}])
        if kind=='nozzle_size' and state in ('RUNNING','PAUSE','PREPARE'):raise ValueError('Change the nozzle setting only when no print is running.')
        if kind=='filament' and state in ('RUNNING','PAUSE','PREPARE'):
            item=param[0];ams=data.get('ams') if isinstance(data.get('ams'),dict) else {}
            active=ams.get('tray_now')
            in_use=(active in (254,255,'254','255')) if item['ams_id'] in (EXTERNAL_SPOOL,EXTERNAL_LEFT) else str(active)==str(item['ams_id']*4+item['slot_id'])
            # Dual-nozzle printers report what each nozzle has loaded (filament_sides.py).
            in_use=in_use or (item['ams_id'],item['slot_id']) in filament_sides.loaded_slots(data)
            if in_use:raise ValueError('That slot is feeding the current print. Change it after the print.')
        if self.core.EXAMPLE_MODE:
            d=self.core.EXAMPLE_DATA[name]
            if kind in ('nozzle','bed'):d[kind+'_target_temper']=int(value)
            elif kind in ('nozzle_left','nozzle_right'):
                side=param[0]['extruder_index']
                for item in ((d.get('device') or {}).get('extruder') or {}).get('info') or []:
                    if item.get('id')==side:item['temp']=(int(value)<<16)|(int(item.get('temp') or 0)&0xFFFF)
            elif kind=='speed':d['spd_lvl']=SPEEDS[value]
            elif kind=='chamber':d['ctt']=int(value)
            elif kind=='filament':
                item=param[0];tray={'tray_type':item['tray_type'],'tray_color':item['tray_color'],'remain':100}
                if item['ams_id'] in (EXTERNAL_SPOOL,EXTERNAL_LEFT) and d.get('vir_slot'):
                    for spool in d['vir_slot']:
                        if filament_sides.external_id(spool.get('id'))==item['ams_id']:spool.update(tray)
                elif item['ams_id']==EXTERNAL_SPOOL:d['vt_tray']={**d.get('vt_tray',{}),**tray}
                else:
                    units=d.get('ams');units=units.get('ams',[]) if isinstance(units,dict) else units
                    for unit in units:
                        if str(unit.get('id'))==str(item['ams_id']):
                            for t in unit.get('tray',[]):
                                if str(t.get('id'))==str(item['slot_id']):t.update(tray)
            elif kind=='nozzle_size':d['nozzle_diameter']=str(param[0]['nozzle_diameter']);d['nozzle_type']=param[0]['nozzle_type']
            elif kind.startswith('fan'):
                targets=d.setdefault('demo_fan_targets',{})
                for c in param:
                    fan=c.get('fan_index')
                    if fan is None:fan=int(re.search(r'M106 P(\d+)',c.get('param',''))[1])
                    targets[str(fan)]=int(value)
        else:
            client=self.core.clients.get(name)
            if not client or not client.is_connected():raise ValueError('Printer disconnected.')
            commands=param if command=='batch' else [{'command':command,'param':param}]
            submitted=0
            for item in commands:
                self.pending={k:v for k,v in self.pending.items() if time.monotonic()-v[3]<300}
                sequence=str(time.time_ns()%1000000000)
                self.pending[sequence]=(name,item['command'],label,time.monotonic())
                item=dict(item);root=item.pop('_root','print')
                payload={root:dict(item,sequence_id=sequence)}
                result=client.publish(f"device/{self.core.printer_config(name)['serial']}/request",json.dumps(payload),qos=1)
                if result.rc!=0:raise ValueError(f'MQTT submission failed after {submitted}/{len(commands)} commands. Inspect actual fan/settings state before retrying.')
                submitted+=1
            if hasattr(self.core,'request_report'):self.core.request_report(name)   # show the result sooner (#60)
        if kind in ('move','home'):self.moved[name]=time.monotonic()
        self.store.event(name,'Demo control' if self.core.EXAMPLE_MODE else 'Control submitted',f'{label} • {author}')
        return label+(' • Demo only.' if self.core.EXAMPLE_MODE else ' • Submitted; verify the result on the printer.')
