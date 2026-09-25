"""Fan capability mapping from Bambu Studio DevFan, and chamber target control."""
FAN_NAMES={1:'Part cooling',2:'Auxiliary cooling',3:'Chamber / exhaust',6:'Internal circulation',10:'Auxiliary cooling 2'}
FAN_KEYS={1:'part',2:'auxiliary',3:'chamber',6:'circulation',10:'auxiliary2'}


def model_name(core,name):
    config=getattr(core,'printer_config',lambda n:None)(name) or {}
    return str(config.get('model') or name).lower().replace(' ','').replace('_','')


def fans(core,name):
    data=core.state_data(name)[2]
    device=data.get('device') or {};duct=device.get('airduct') if isinstance(device,dict) else None
    if isinstance(duct,dict) and isinstance(duct.get('parts'),list):
        current=next((m for m in duct.get('modeList',[]) if m.get('modeId')==duct.get('modeCur')), {})
        allowed={int(v)>>4 for v in current.get('ctrl',[])};off={int(v)>>4 for v in current.get('off',[])}
        result=[]
        for p in duct['parts']:
            try:
                packed=int(p['id']);index=(packed>>4)&255;kind=packed&15;func=int(p.get('func',index));r=int(p['range'])
                if kind!=0 or index in (0,4,5):continue # firmware-controlled heatbreak/electronics
                low=r&65535;high=(r>>16)&65535
                if not 0<=low<=high<=100:continue
                result.append({'key':FAN_KEYS.get(index,'id'+str(index)),'id':index,'label':FAN_NAMES.get(func,'Fan '+str(index)),
                    'protocol':'set_fan','minimum':low,'maximum':high,'manual':index in allowed and index not in off,
                    'percent':int(p.get('state',0))&255})
            except (ValueError,TypeError,KeyError):continue
        return result
    model=model_name(core,name);indices=[1]
    if data.get('support_aux_fan') is True or any(m in model for m in ('h2d','x1','p1s')):indices.append(2)
    if data.get('support_chamber_fan') is True or any(m in model for m in ('h2d','x1','p1s')):indices.append(3)
    return [{'key':FAN_KEYS[i],'id':i,'label':FAN_NAMES[i],'protocol':'gcode_line','minimum':0,'maximum':100,'manual':True,'percent':None} for i in indices]


def fan_commands(core,name,target,percent):
    available=fans(core,name)
    if target=='all':selected=[f for f in available if f['manual']]
    else:selected=[f for f in available if f['key']==target]
    if not selected:raise ValueError('No matching manually controllable fan reported. Check printer telemetry and its airflow mode.')
    commands=[]
    for fan in selected:
        if not fan['manual']:raise ValueError(f"{fan['label']} is automatic/off in the current airflow mode. Change the mode on the printer first.")
        if not fan['minimum']<=percent<=fan['maximum']:raise ValueError(f"{fan['label']} accepts {fan['minimum']}–{fan['maximum']}% in this mode.")
        if fan['protocol']=='set_fan':commands.append({'command':'set_fan','fan_index':fan['id'],'speed':percent})
        else:commands.append({'command':'gcode_line','param':f"M106 P{fan['id']} S{round(percent*255/100)}\n"})
    label=', '.join(f['label'] for f in selected)+f' → {percent}%'
    skipped=[f['label'] for f in available if not f['manual']]
    if target=='all' and skipped:label+=' • Automatic/off fans unchanged: '+', '.join(skipped)
    return commands,label
