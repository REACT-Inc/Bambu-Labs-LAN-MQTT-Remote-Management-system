"""Import literal settings from the existing bot without executing it."""
import argparse
import ast
import json
import secrets
from pathlib import Path
from dashboard import atomic_json, password_hash


def migrate(old_file, config_path, data_dir, listen, demo=False, enable_reboot=False):
    config_path=Path(config_path);data_dir=Path(data_dir)
    data_dir.mkdir(parents=True,exist_ok=True)
    if not config_path.exists():
        config_path.parent.mkdir(parents=True,exist_ok=True)
        values={};nonempty_printers=None
        if Path(old_file).exists():
            wanted={'DISCORD_BOT_TOKEN','ALLOWED_GUILD_IDS','SETTINGS_USER_IDS','SETTINGS_FILE','PRINTERS','EXAMPLE_DATA'}
            for node in ast.parse(Path(old_file).read_text()).body:
                if isinstance(node,ast.Assign):
                    for target in node.targets:
                        if isinstance(target,ast.Name) and target.id in wanted:
                            try:value=ast.literal_eval(node.value)
                            except (ValueError,SyntaxError):
                                raise ValueError(f'{target.id} uses a computed value. Convert it to a literal setting before importing.')
                            values[target.id]=value
                            if target.id=='PRINTERS' and value:nonempty_printers=value
            if not all(k in values for k in ('DISCORD_BOT_TOKEN','ALLOWED_GUILD_IDS','PRINTERS')):
                raise ValueError('Existing bot is missing token, server IDs or printer settings.')
        elif not demo:
            raise ValueError('Existing bot not found. Use --demo for a new dashboard-only demo installation.')
        token=values.get('DISCORD_BOT_TOKEN','')
        if token.startswith('REPLACE_'):token=''
        config=dict(discord_token=token,guild_ids=list(values.get('ALLOWED_GUILD_IDS',[])),
            admin_user_ids=list(values.get('SETTINGS_USER_IDS',[])),printers=nonempty_printers or values.get('PRINTERS',[]),
            demo=demo or not values.get('PRINTERS'),listen=list(dict.fromkeys(['127.0.0.1',listen])),port=8080)
        if len({p['name'].casefold() for p in config['printers']})!=len(config['printers']):
            raise ValueError('Each printer needs a unique name.')
        if not config['demo']:
            for printer in config['printers']:
                if any(not printer.get(k) for k in ('name','ip','serial','access_code')):
                    raise ValueError('Each live printer requires name, ip, serial and access_code.')
        atomic_json(config_path,config)
        settings_source=Path(values.get('SETTINGS_FILE','/tmp/bot_settings.json'))
        if settings_source.exists() and not (data_dir/'settings.json').exists():
            atomic_json(data_dir/'settings.json',json.loads(settings_source.read_text()))
        print('Imported existing configuration.' if values else 'Created a dashboard-only demo configuration.')
        if config['demo']:print('DEMO MODE is enabled. Real printer controls are disabled until demo is false in config.json.')
    else:
        print('Existing management configuration preserved.')
    if enable_reboot:
        existing=json.loads(config_path.read_text());existing['allow_host_reboot']=True;atomic_json(config_path,existing)
    if not (data_dir/'auth.json').exists():
        password=secrets.token_urlsafe(18)
        atomic_json(data_dir/'auth.json',password_hash(password))
        print('\nINITIAL DASHBOARD PASSWORD: '+password+'\nSave this password. Change it in dashboard Settings.\n')


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--old',default='/tmp/printer_discord_bot.py')
    parser.add_argument('--config',default='/etc/3d-printer-management/config.json')
    parser.add_argument('--data',default='/var/lib/3d-printer-management')
    parser.add_argument('--listen',default='127.0.0.1')
    parser.add_argument('--demo',action='store_true')
    parser.add_argument('--enable-reboot',action='store_true')
    args=parser.parse_args()
    migrate(args.old,args.config,args.data,args.listen,args.demo,args.enable_reboot)
