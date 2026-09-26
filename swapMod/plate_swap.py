import json
import time


class PlateSwap:
    def __init__(self,core,store):
        self.core,self.store=core,store
        store.db.execute('''CREATE TABLE IF NOT EXISTS plate_swap (
            printer TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 0,
            model TEXT NOT NULL DEFAULT '', spares INTEGER NOT NULL DEFAULT 0,
            verified INTEGER NOT NULL DEFAULT 0, revision INTEGER NOT NULL DEFAULT 0)''')
        if 'provider' not in {r[1] for r in store.db.execute('PRAGMA table_info(plate_swap)')}:
            store.db.execute("ALTER TABLE plate_swap ADD COLUMN provider TEXT NOT NULL DEFAULT ''")
        # Retire previous Infinity Flow settings rather than reinterpret them.
        store.db.execute("UPDATE plate_swap SET enabled=0,model='',spares=0,verified=0 WHERE provider!='swapmod_a1m'")
        store.db.execute("UPDATE plate_swap SET provider='swapmod_a1m'")
        # A restart loses evidence of the physical plate state.
        store.db.execute('UPDATE plate_swap SET verified=0,revision=revision+1')
        store.db.commit()

    def state(self,name):
        row=self.store.db.execute('SELECT * FROM plate_swap WHERE printer=?',(name,)).fetchone()
        result=dict(row) if row else dict(printer=name,enabled=0,model='',spares=0,verified=0,revision=0)
        result['enabled']=bool(result['enabled']);result['verified']=bool(result['verified'])
        result['provider']='swapmod_a1m'
        result['mode']='Prepared Swaplist batch; confirm setup before each batch'
        return result

    def configure(self,name,enabled,model,spares,confirmed,author):
        if name not in self.core.names():raise ValueError('Unknown printer.')
        if type(enabled) is not bool or type(spares) is not int or not 0<=spares<=100:
            raise ValueError('Choose on/off and a magazine plate count from 0 to 100.')
        if confirmed is not True:raise ValueError('Confirm the installed kit and actual magazine plate count.')
        if self.store.active(name):raise ValueError('Finish or resolve the active job before changing plate-swap settings.')
        if enabled:
            if model!='A1 mini':raise ValueError('Swapmod A1m supports the A1 mini only.')
            p=self.core.printer_config(name) or {}
            hint=str(p.get('model') or name).lower().replace(' ','')
            if any(x in hint for x in ('h2d','x1','p1','a2l')):raise ValueError('This printer is not supported by Swapmod A1m.')
            if 'a1' in hint and 'mini' not in hint:
                raise ValueError('Swapmod A1m requires an A1 mini, not the full-size A1.')
        previous=self.state(name)
        with self.store.db:
            self.store.db.execute('INSERT OR REPLACE INTO plate_swap(printer,enabled,model,spares,verified,revision,provider) VALUES(?,?,?,?,0,?,?)',
                (name,int(enabled),model if enabled else '',spares,previous['revision']+1,'swapmod_a1m'))
        self.store.event(name,'Plate-swap settings',f'{"Enabled" if enabled else "Disabled"} • {spares} magazine plates • {author}')
        return self.state(name)

    def approve_job(self,job_id,confirmed,author,plates=1):
        if type(plates) is not int or not 1<=plates<=100:raise ValueError("Enter the batch plate count (1–100).")
        if confirmed is not True:raise ValueError('Confirm this is a Swaplist-generated batch for Swapmod A1m and verify its total plate count.')
        job=self.store.get(job_id);cfg=self.state(job['printer'])
        if not cfg['enabled']:raise ValueError('Enable the installed plate-swap kit first.')
        if job['status']!='queued':raise ValueError('Only waiting jobs can be approved.')
        if 'swap_model' in job['options'] and job['options'].get('swap_provider')!='swapmod_a1m':
            raise ValueError('This job was approved for another swap system. Add a newly generated Swaplist batch instead.')
        opts=dict(job['options']);opts['swap_revision']=cfg['revision'];opts['swap_model']=cfg['model'];opts['swap_provider']='swapmod_a1m';opts['swap_plates']=plates
        with self.store.db:
            self.store.db.execute('UPDATE jobs SET options=?,updated=? WHERE id=?',(json.dumps(opts),time.time(),job_id))
        self.store.event(job['printer'],'Swaplist batch attested',f'{job_id} • {author} • user attestation, not automatic G-code validation')

    def verify(self,name,confirmed,author):
        if confirmed is not True:raise ValueError('Check the printer starting setup and loaded magazine against the Swaplist instructions; clear the plate ejection path.')
        if not self.state(name)['enabled']:raise ValueError('Plate-swap mode is disabled.')
        if self.store.active(name):raise ValueError('Finish or resolve the active job before checking the plate.')
        with self.store.db:self.store.db.execute('UPDATE plate_swap SET verified=1 WHERE printer=?',(name,))
        self.store.event(name,'Swapmod starting setup checked',author)

    def check(self,job,dispatch=False):
        cfg=self.state(job['printer']);opts=job['options']
        if not cfg['enabled']:
            if 'swap_model' in opts:raise ValueError('This job contains an approved swap preset, but plate-swap mode is now disabled. Add a newly sliced standard job.')
            return
        if opts.get('swap_revision')!=cfg['revision'] or opts.get('swap_model')!=cfg['model'] or opts.get('swap_provider')!='swapmod_a1m':
            raise ValueError('Approve this job as a Swaplist-generated Swapmod A1m batch and enter its plate count.')
        if not dispatch and not cfg['verified']:raise ValueError('Check the Swapmod starting setup using /plateswap check or the dashboard first.')
        if not dispatch and cfg['spares']<opts['swap_plates']:raise ValueError('Not enough magazine plates for this batch. Refill and update the count.')

    def reserve(self,job):
        if self.state(job['printer'])['enabled']:
            # Called inside the same transaction as queue claim. Never auto-refund uncertain outcomes.
            self.store.db.execute('UPDATE plate_swap SET spares=spares-?,verified=0 WHERE printer=?',(job['options']['swap_plates'],job['printer']))

    def invalidate(self,name):
        with self.store.db:self.store.db.execute('UPDATE plate_swap SET verified=0 WHERE printer=?',(name,))
