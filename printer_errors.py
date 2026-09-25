"""Exact error-code lookups from bundled official Bambu Studio resources."""
import json
from pathlib import Path

_catalogs=json.loads(Path(__file__).with_name('bambu_error_catalog.json').read_text())['catalogs']
_maps={}
for model,catalog in _catalogs.items():
    _maps[model]={}
    for kind in ('device_error','device_hms'):
        _maps[model][kind]={str(e['ecode']).upper():e.get('intro','').strip() for e in catalog.get('data',{}).get(kind,{}).get('en',[]) if e.get('ecode')}


def number(value):
    if isinstance(value,str):
        return int(value,16) if value.lower().startswith('0x') or any(c in 'abcdefABCDEF' for c in value) else int(value)
    return int(value)


def lookup(code,kind='device_error',model=None):
    code=code.upper()
    message=_maps.get(model,{}).get(kind,{}).get(code,'')
    if not message:
        variants={m[kind].get(code,'') for m in _maps.values()}-{''}
        if len(variants)==1:message=variants.pop()
    return dict(code=code,message=message or 'Unrecognized error or model-specific wording; open Bambu support for details.',url='https://e.bambulab.com/?e='+code)


def errors(error,data=None,model=None):
    result=[]
    try:
        n=number(error)
        if 0<n<=0xffffffff:result.append(lookup(f'{n:08X}',model=model))
    except (TypeError,ValueError):pass
    for h in (data or {}).get('hms',[]) or []:
        if not isinstance(h,dict):continue
        try:
            attr,code=number(h.get('attr',0)),number(h.get('code',0))
            if not 0<attr<=0xffffffff or not 0<=code<=0xffffffff:continue
            result.append(lookup(f'{attr:08X}{code:08X}','device_hms',model))
        except (TypeError,ValueError):continue
    return result


def describe(error,data=None,model=None):
    return '\n'.join(f"{e['message']} [Code {e['code']}]\n{e['url']}" for e in errors(error,data,model)) or ('No active error.' if not error else f'Unrecognized printer error: {error}')
