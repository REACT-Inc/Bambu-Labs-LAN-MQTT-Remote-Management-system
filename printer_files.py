"""Read-only, bounded printer FTPS browsing."""
import asyncio
import ftplib
import posixpath
import ssl
import time


def normalize(path):
    if not isinstance(path,str) or len(path)>500 or any(ord(c)<32 for c in path) or '..' in path.split('/'):
        raise ValueError('Use a printer folder path without .. or control characters.')
    return '/' + path.strip('/')


def scan(printer, path='/', limit=2000):
    from bambulabs_api.ftp_client import ImplicitFTP_TLS
    path=normalize(path);deadline=time.monotonic()+45
    ftp=ImplicitFTP_TLS(timeout=5,context=ssl._create_unverified_context())
    entries=[];warnings=[];pending=[(path,0)];seen=set()
    def check():
        if time.monotonic()>deadline:raise TimeoutError('Listing time limit reached; use a smaller folder.')
    try:
        ftp.connect(printer['ip'],990);ftp.login('bblp',printer['access_code']);ftp.prot_p()
        while pending and len(entries)<limit:
            check();folder,depth=pending.pop(0)
            if folder in seen:continue
            seen.add(folder)
            try:
                ftp.cwd(folder)
                rows=[]
                def collect(line):
                    check()
                    if len(rows)>=limit:raise ValueError('Directory too large; browse a smaller folder.')
                    rows.append(line)
                # LIST is supported by Bambu servers that do not implement MLSD.
                ftp.retrlines('LIST',collect)
                for line in rows:
                    parts=line.split(None,8)
                    if len(parts)!=9 or parts[0][0] not in 'dl-':
                        warnings.append('Unrecognized listing entry in '+folder);continue
                    kind='directory' if parts[0][0]=='d' else 'link' if parts[0][0]=='l' else 'file'
                    name=parts[8].split(' -> ',1)[0] if kind=='link' else parts[8]
                    if name in ('.','..'):continue
                    if '/' in name or any(ord(c)<32 for c in name):continue
                    full=posixpath.join(folder,name)
                    entries.append(dict(path=full,type=kind,size=int(parts[4]) if parts[4].isdigit() else None))
                    if kind=='directory':
                        if depth<10:pending.append((full,depth+1))
                        else:warnings.append('Depth limit reached at '+full)
                    if len(entries)>=limit:break
            except ftplib.error_perm as exc:
                warnings.append(f'{folder}: {str(exc)[:160]}')
        if pending or len(entries)>=limit:warnings.append('Listing truncated at 2000 entries; choose a smaller folder.')
    except (TimeoutError,ValueError) as exc:
        warnings.append(str(exc))
    finally:
        ftp.close()
    return dict(root=path,entries=entries,warnings=warnings)


class Browser:
    def __init__(self,core):self.core=core;self.tasks={}
    async def browse(self,name,path='/'):
        path=normalize(path)
        if self.core.EXAMPLE_MODE:
            return dict(root=path,entries=[dict(path='/demo.gcode.3mf',type='file',size=0)],warnings=['Demo listing.'])
        if name in self.tasks and not self.tasks[name].done():
            raise ValueError('A file listing is already running for this printer. Try again shortly.')
        printer=self.core.printer_config(name)
        if not printer:raise ValueError('Unknown printer.')
        task=asyncio.create_task(asyncio.to_thread(scan,printer,path));self.tasks[name]=task
        try:return await asyncio.wait_for(asyncio.shield(task),60)
        except asyncio.TimeoutError:raise ValueError('Printer file listing timed out.')


def render(result, printable=False):
    entries=result['entries']
    if printable:entries=[e for e in entries if e['type']=='file' and e['path'].lower().endswith(('.3mf','.gcode'))]
    lines=[('DIR  ' if e['type']=='directory' else 'LINK ' if e['type']=='link' else 'FILE ')+e['path'] for e in entries]
    return '\n'.join([f"Printer folder: {result['root']}"]+lines+(['No matching files or folders.'] if not lines else [])+['NOTICE: '+w for w in result['warnings']])
