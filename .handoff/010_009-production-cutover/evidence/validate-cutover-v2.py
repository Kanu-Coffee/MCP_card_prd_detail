import importlib.util
import json
from pathlib import Path
import tempfile
from unittest.mock import patch
from types import SimpleNamespace
spec=importlib.util.spec_from_file_location('cutover',str(Path(__file__).with_name('cutover-v2.py')))
m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
for failure in [False,True]:
    with tempfile.TemporaryDirectory() as directory:
        root=Path(directory); old=root/'old'; new=root/'new'; current=root/'current'
        for snapshot in [old,new]:
            (snapshot/'operations').mkdir(parents=True)
            for service in ['mcp','worker']:
                p=snapshot/'deploy'/service; p.mkdir(parents=True)
                (p/'compose.secrets.yaml').write_text('services:\n  '+service+':\n    image: sample\n    environment:\n      SAMPLE: preserved\nsecrets:\n  auth:\n    file: /sample\n')
            (snapshot/'operations/mcp-compose.sh').write_text('compose -p cardrag-stable-v1026 ')
        current.symlink_to(old)
        volumes={}; inodes={}
        for oldname,newname in m.PAIRS:
            p=root/oldname; p.mkdir(); (p/'data').write_text(oldname); (p/'folder').mkdir(); (p/'folder/x').write_text('x')
            volumes[oldname]=p; inodes[newname]=(p/'data').stat().st_ino
        def inspect(name):
            return {'Driver':'local','Options':None,'Mountpoint':str(volumes[name])}
        def run(*args,capture=False,check=True):
            result=''
            if args[:3]==('systemctl','is-active','cardrag-worker.service'): result='inactive\n'
            if args[:3]==('docker','volume','create'):
                p=root/args[3]; p.mkdir(); volumes[args[3]]=p
            return SimpleNamespace(stdout=result)
        checks=iter([False,True] if failure else [True])
        with patch.multiple(m,OLD=old,NEW=new,CURRENT=current,JOURNAL=new/'operations/journal.json'),patch.object(m,'run',run),patch.object(m,'inspect',inspect),patch.object(m,'ready',lambda:next(checks)),patch.object(m.os,'geteuid',lambda:0):
            try: m.main()
            except RuntimeError:
                assert failure
            state=json.loads(m.JOURNAL.read_text())
            assert state['status']==('restored' if failure else 'deployed')
            assert current.resolve()==(old if failure else new)
            for oldname,newname in m.PAIRS:
                destination=volumes[oldname if failure else newname]
                assert (destination/'data').stat().st_ino==inodes[newname]
                assert (destination/'folder/x').read_text()=='x'
            for snapshot in [old,new]:
                text=(snapshot/'deploy/worker/compose.secrets.yaml').read_text()
                assert 'SAMPLE: preserved' in text
                assert ('name: cardrag-worker-state' in text)==(not failure)
        print(('rollback' if failure else 'successful migration')+': passed, file inodes/content preserved')
