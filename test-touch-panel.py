#!/usr/bin/env python3
"""HDMI 1024x600 panel: touches map into the letterboxed 1280x800 image; bars are ignored."""
import os,struct,subprocess,tempfile
from pathlib import Path
source=Path(__file__).resolve().parent
EV=struct.Struct('@llHHi');REPORT=struct.Struct('<BBHH')
def event(t,c,v):return EV.pack(0,0,t,c,v)
def tap(x,y):
 return b''.join(event(*e) for e in [(3,0x2f,0),(3,0x39,7),(3,0x35,x),(3,0x36,y),(0,0,0),(3,0x39,-1),(0,0,0)])
def native(ux,uy):return (37+(1280-ux)*3976//1280,72+uy*3856//800)
with tempfile.TemporaryDirectory() as tmp:
 d=Path(tmp);binary=d/'bridge';state=d/'state';control=d/'control';control.touch()
 subprocess.run(['gcc','-O2',f'-DUI_STATE="{state}"',f'-DUI_CONTROL="{control}"','-o',str(binary),str(source/'touch-bridge.c')],check=True)
 env=dict(os.environ,RX3_PANEL='hdmi-1024x600')
 def run(x,y):
  out=d/'out';out.write_bytes(b'')
  p=subprocess.run([str(binary),'--replay',str(out),'--fullscreen'],input=tap(x,y),env=env,capture_output=True,timeout=5)
  assert p.returncode==0,p.stderr
  raw=out.read_bytes();reports=[REPORT.unpack_from(raw,i) for i in range(0,len(raw)-5,6)]
  return {(r[2],r[3]) for r in reports if r[0]}
 # Panel pixel -> RX3 pixel: x'=(x-32)*4/3, y'=y*4/3.
 for (x,y),(ux,uy) in {(32,0):(0,0),(512,300):(640,400),(991,599):(1278,798)}.items():
  assert run(x,y)=={native(ux,uy)},(x,y,run(x,y))
 for x,y in ((0,300),(31,300),(992,300),(1023,0)):
  assert run(x,y)==set(),(x,y)
 p=subprocess.run([str(binary),'--replay',str(d/'out')],input=b'',env=env,capture_output=True,timeout=5)
 assert p.returncode==2 and b'only --fullscreen' in p.stderr
print('PASS HDMI 1024x600: corners and centre map into 1280x800, black bars ignored, full screen only')
