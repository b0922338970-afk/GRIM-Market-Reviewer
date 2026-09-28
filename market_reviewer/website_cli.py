"""CLI for the GRIM V4.5 read-only website."""
from __future__ import annotations
import argparse,json
from pathlib import Path
from .persistence import atomic_write_json
from .website_read_model import build_website_read_model

def build_parser():
    p=argparse.ArgumentParser(prog='grim-website'); s=p.add_subparsers(dest='command')
    s.add_parser('status',help='Print current website read model; no mutation')
    e=s.add_parser('export',help='Export one website snapshot'); e.add_argument('--output',default='artifact/website-read-model.json')
    public=s.add_parser('export-public',help='Atomically export public-safe website projection')
    public.add_argument('--output',default='artifact/website-public-snapshot.json')
    public.add_argument('--stale-after-seconds',type=int,default=1800)
    r=s.add_parser('serve',help='Serve local read-only dashboard'); r.add_argument('--host',default='127.0.0.1'); r.add_argument('--port',type=int,default=8765); r.add_argument('--web-root',default='web')
    return p

def main(argv=None):
    p=build_parser(); a=p.parse_args(argv)
    if a.command=='status': print(json.dumps(build_website_read_model(),ensure_ascii=False,indent=2,sort_keys=True)); return 0
    if a.command=='export': path=Path(a.output); atomic_write_json(path,build_website_read_model()); print(path); return 0
    if a.command=='export-public':
        from .website_public_snapshot import export_public_snapshot
        result=export_public_snapshot(output=Path(a.output),stale_after_seconds=a.stale_after_seconds)
        print(json.dumps(result,sort_keys=True))
        return 0 if result['status']=='PUBLISHED' else 1
    if a.command=='serve':
        from .website_server import serve
        serve(a.host,a.port,Path(a.web_root)); return 0
    p.print_help(); return 0
if __name__=='__main__': raise SystemExit(main())
