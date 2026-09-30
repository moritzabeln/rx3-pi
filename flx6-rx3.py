#!/usr/bin/env python3
"""Use BiteDJ's installed MIDI definitions to feed the RX3 native key queue.
Hot-cue, beat-loop and default beat-jump banks supported. LED feedback for profiles with an LED map.
"""
import argparse, ctypes, errno, json, os, re, signal, struct, subprocess, time
import xml.etree.ElementTree as ET
BUTTONS={'play':0x4101,'cue_default':0x4102,'loop_in':0x410c,'loop_out':0x410d,
 'reloop_toggle':0x410e,'slip_enabled':0x4110,'sync_enabled':0x4112,'sync_leader':0x4111,
 'keylock':0x4108,'PioneerDDJFLX6.cycleTempoRange':0x4107,'quantize':0x410b,'pfl':0x5020,'LoadSelectedTrack':0x4311,
 'MoveFocusForward':0x420c,'MoveFocusBackward':0x420d,'PioneerDDJFLX6.shiftPressed':0x4103,
 # Native RX3 keys, reached through profile aliases.
 'CallPrev':0x4323,'CallNext':0x4322,'CueDelete':0x4124,'CueMemory':0x4125,'Browse':0x202}
ANALOG={'pregain':0x5019,'parameter3':0x501a,'parameter2':0x501b,'parameter1':0x501c,
 'volume':0x501e,'super1':0x509d,'crossfader':0x6017,'headMix':0x4405}
PAD_BANKS={"pad-hotcue":0,"pad-beatloop":1,"pad-beatjump":3}
LOOP_SIZES=("0.25","0.5","1","2","4","8","16","32")
# jog_scale converts platter ticks to the FLX6's 7200 ticks per revolution.
PROFILES={
 'DDJ-FLX6':dict(prefix='PioneerDDJFLX6.',jog_scale=1,zoom_sign=1,aliases={},extra=(),leds=None),
 # Mixxx's Pioneer-DDJ-400 mapping; 720 ticks/revolution as in its scratchEnable().
 'DDJ-400':dict(prefix='PioneerDDJ400.',jog_scale=10,zoom_sign=-1,
  aliases={'PioneerDDJ400.syncPressed':'sync_enabled','PioneerDDJ400.syncLongPressed':'sync_leader',
   'MoveVertical':'PioneerDDJ400.browseRotate','MoveFocusBackward':'PioneerDDJ400.backPressed',
   # CUE/LOOP CALL: the RX3 calls memory cues, or halves/doubles an active loop.
   'PioneerDDJ400.cueLoopCallLeft':'CallPrev','PioneerDDJ400.cueLoopCallRight':'CallNext',
   # Shift + CUE/LOOP CALL (Mixxx: 32-beat jumps) as in rekordbox: delete / memory.
   'PioneerDDJ400.quickJumpBack':'CueDelete','PioneerDDJ400.quickJumpForward':'CueMemory'},
  # Official MIDI list; unmapped in Mixxx: Shift + browse turn, Shift + LOAD deck 2.
  # The plain native Browse key toggles player/library (FLX6 VIEW only opens it).
  extra=(('[Library]','PioneerDDJ400.waveformZoom','0xb6','0x64',set()),
   ('[Tab]','Browse','0x96','0x7a',set())),
  # RX3 panel LED id -> DDJ-400 LED notes (official MIDI list); the Shift layer mirrors them.
  leds=dict(deck={1:(0x0b,0x47),2:(0x0c,0x48),4:(0x58,),7:(0x10,0x4c),8:(0x11,0x4e),9:(0x4d,0x50),50:(0x54,0x68)},
   # Active RX3 pad mode (HotCue, AutoBeatLoop, BeatJump LED) -> DDJ-400 pad note base.
   banks={14:0x00,15:0x60,17:0x20},pads=(0x97,0x99),common={51:(0x96,0x63)},meter=0x02))}
class Bridge:
 def __init__(self,xml,emit,clock=time.monotonic,model='DDJ-FLX6'):
  profile=PROFILES[model];prefix=profile['prefix'];self.jog_scale=profile['jog_scale'];self.zoom_sign=profile['zoom_sign']
  self.leds=profile['leds'];self.led_sent={}
  self.clock=clock;self.jogs={ch:dict(total=0,delta=0,last=clock(),moved=0,speed=0) for ch in (1,2)}
  self.emit=emit;self.mapping={};self.msb={};self.held=set();self.pad_held={};self.status=None;self.data=[]
  self.shift={1:False,2:False}
  self.grid_ticks={1:0,2:0}
  controls=[(c.findtext('group',''),c.findtext('key',''),c.findtext('status'),c.findtext('midino'),
   {o.tag for o in c.findall('./options/*')}) for c in ET.parse(xml).findall('.//controls/control')]
  for g,key,status,midino,opts in controls+list(profile['extra']):
   key=profile['aliases'].get(key,key)
   # Script names below use the FLX6 spelling.
   if key.startswith(prefix):key='PioneerDDJFLX6.'+key[len(prefix):]
   match=re.search(r'\[Channel(\d)\]',g)
   if match:
    channel=int(match[1])
    if channel>2:continue
   elif g in ('[Master]','[Library]','[Tab]'):channel=0
   else:continue
   mode='button';native=BUTTONS.get(key)
   if key=='PioneerDDJFLX6.shiftPressed':mode='shift'
   if g=='[Tab]' and key in ('library','PioneerDDJFLX6.viewPressed'):native=0x202;mode='view'
   if g=='[Library]' and key=='MoveFocusForward':native=0x420c;mode='browse-push'
   if key=='PrepareView':native=0x203;mode='prepare-view'
   if key=='PrepareToggle':native=0x420e;mode='prepare-toggle'
   if key=='PioneerDDJFLX6.backPressed':native=0x420d;mode='back'
   if key in ANALOG:native=ANALOG[key];mode='analog'
   if key=='PioneerDDJFLX6.jogTurn':native=0x4305;mode='jog'
   if key=='PioneerDDJFLX6.jogSearch':native=0;mode='grid-jog'
   if key=='PioneerDDJFLX6.jogTouch' and int(midino,0)==0x36:native=0x4306
   if key=='PioneerDDJFLX6.tempoSliderMSB':native=0x4109;mode='tempo-msb'
   if key=='PioneerDDJFLX6.tempoSliderLSB':native=0x4109;mode='tempo-lsb'
   if key=='PioneerDDJFLX6.browseRotate':native=0x420c;mode='relative'
   if key=='PioneerDDJFLX6.waveformZoom':native=0x420c;mode='waveform-zoom';channel=0
   pad=re.fullmatch(r'hotcue_([1-8])_activate',key)
   if pad:native=0x4116+int(pad[1]);mode="pad-hotcue"
   if key=="PioneerDDJFLX6.beatjumpPadPressed":
    note=int(midino,0)
    if 0x20<=note<=0x27:native=0x4117+note-0x20;mode="pad-beatjump"
   loop=re.fullmatch(r'beatloop_(0\.25|0\.5|1|2|4|8|16|32)_toggle',key)
   if loop:native=0x4117+LOOP_SIZES.index(loop[1]);mode="pad-beatloop"
   if native is None:continue
   if 'fourteen-bit-msb' in opts:mode='msb'
   if 'fourteen-bit-lsb' in opts:mode='lsb'
   addr=(int(status,0),int(midino,0))
   item=(native,channel,mode)
   if addr in self.mapping and self.mapping[addr]!=item:raise ValueError(f'Conflicting mapping {addr}')
   self.mapping[addr]=item
 def message(self,status,note,value):
  off=status&0xf0==0x80
  if off:status+=0x10;value=0
  item=self.mapping.get((status,note))
  if not item:return
  key,ch,mode=item
  if mode in ('prepare-view','prepare-toggle'):
   if value:self.emit(key,0,ch,0,0.,0x4254 if mode=='prepare-view' else 0x4251)
  elif mode=='browse-push':
   if value:self.emit(key,0,ch,0,0.,0x4250)
  elif mode in ('view','back'):
   if value:self.emit(key,0,ch,0,0.,0x4256 if mode=='view' else 0x424b)
  elif mode=='shift':
   down=bool(value);self.shift[ch]=down
   self.grid_ticks[ch]=0
   if down:
    self.stop_jog(ch)
    if (0x4306,ch) in self.held:
     self.emit(0x4306,2,ch,0,0.,0);self.held.discard((0x4306,ch))
   if down:self.held.add((key,ch))
   else:self.held.discard((key,ch))
   self.emit(key,0 if down else 2,ch,0,0.,0)
  elif mode=='grid-jog':self.grid_jog(ch,value)
  elif mode=='jog':
   if self.shift[ch]:self.grid_jog(ch,value);return
   j=self.jogs[ch];delta=(value-64)*self.jog_scale
   if delta:j['delta']+=delta;j['total']+=delta;j['moved']=self.clock()
  elif mode in PAD_BANKS:
   bank=PAD_BANKS[mode];identity=(key,ch)
   if value:
    if self.pad_held.get(identity)==bank:return
    # Release the old bank before selecting another. Each deck is independent.
    for (old_key,old_ch),old_bank in list(self.pad_held.items()):
     if old_ch==ch and old_bank!=bank:
      self.emit(old_key,2,ch,0,0.,0x5040|old_bank)
      del self.pad_held[(old_key,ch)];self.held.discard((old_key,ch))
    self.pad_held[identity]=bank;self.held.add(identity)
    self.emit(key,0,ch,0,0.,0x5040|bank)
   elif self.pad_held.get(identity)==bank:
    del self.pad_held[identity];self.held.discard(identity)
    self.emit(key,2,ch,0,0.,0x5040|bank)
  elif mode=='button':
   op=0 if value else 2
   if key==0x4306 and self.shift[ch]:return
   if key==0x4306 and op==2:self.stop_jog(ch)
   if op==0:self.held.add((key,ch))
   else:self.held.discard((key,ch))
   self.emit(key,op,ch,0,0.,0)
  elif mode=='waveform-zoom':
   # One zoom step per message; the DDJ-400 sends up to ±30 on a fast turn.
   if value!=64 and value:self.emit(key,4,0,(1 if value>64 else -1)*self.zoom_sign,0.,0x425a)
  elif mode=='relative':
   delta=value if value<64 else value-128
   if delta:self.emit(key,4,ch,delta,0.,0x4252) # browser-only encoder intent
  elif mode in ('msb','tempo-msb'):self.msb[(status,note)]=value
  elif mode=='tempo-lsb':
   hi=self.msb.get((status,note-32))
   if hi is not None:self.emit(key,5,ch,0,((hi<<7)|value)/8192.-1.,0x544d)
  elif mode=='lsb':
   hi=self.msb.get((status,note-32))
   if hi is not None:self.emit(key,4,ch,0,((hi<<7)|value)/16383.,0)
  else:self.emit(key,4,ch,0,value/127.,0)
 def feed(self,data):
  for b in data:
   if b>=0xf8:continue
   if b&0x80:
    self.data=[];self.status=b if b<0xf0 else None
   elif self.status is not None:
    self.data.append(b)
    n=1 if self.status&0xf0 in (0xc0,0xd0) else 2
    if len(self.data)==n:
     if n==2:self.message(self.status,*self.data)
     self.data=[]
 def grid_jog(self,ch,value):
  total=self.grid_ticks[ch]+(value-64)*self.jog_scale
  steps=abs(total)//16*(1 if total>=0 else -1)
  self.grid_ticks[ch]=total-steps*16
  # BiteDJ: 5 ms per step. RX3 offset units are quarter milliseconds.
  if steps:self.emit(0,4,ch,steps*20,0.,0x4744)
 def pulse(self,ch):
  # RX3 hardware uses 1620 pulses/revolution; BiteDJ FLX6 uses 7200.
  return round(self.jogs[ch]['total']*1620/7200)&65535
 def stop_jog(self,ch):
  j=self.jogs[ch];self.emit(0x4305,4,ch,0,0.,self.pulse(ch));j['delta']=0;j['speed']=0;j['last']=self.clock()
 def tick(self):
  now=self.clock()
  for ch,j in self.jogs.items():
   elapsed=now-j['last']
   if elapsed<.01:continue
   if j['delta']:
    # 1x = 33 1/3 RPM = one revolution/1.8 seconds.
    speed=j['delta']*1.8/(7200*elapsed)
    self.emit(0x4305,4,ch,0,speed,self.pulse(ch))
    j['delta']=0;j['speed']=speed;j['last']=now
   elif j['speed'] and now-j['moved']>=.04:self.stop_jog(ch)
   elif not j['speed']:j['last']=now
 def release(self):
  for ch,j in self.jogs.items():
   if j['speed'] or j['delta']:self.stop_jog(ch)
  for key,ch in list(self.held):self.emit(key,2,ch,0,0.,0)
  self.held.clear();self.pad_held.clear()
  for ch in self.shift:self.shift[ch]=False;self.grid_ticks[ch]=0
 def led_frame(self,states):
  """MIDI that brings the controller LEDs to states[channel*64+id] (bit0 lit, bit1 active,
  bit2 dim) and the channel meters to states[192+channel] (lit RX3 segments, 0-11)."""
  if not self.leds:return b''
  leds=self.leds;want={}
  # The DDJ-400 cannot dim; the RX3 dims e.g. pads without a hot cue.
  def on(v):return 0x7f if v&5==1 else 0
  for ch in (1,2):
   row=states[ch*64:ch*64+64];status=0x8f+ch;pad=leds['pads'][ch-1]
   for led,notes in leds['deck'].items():
    for note in notes:want[(status,note)]=on(row[led])
   bank=next((base for led,base in leds['banks'].items() if row[led]&2),None)
   # RX3 pads 1-8 are LEDs 18-25; only the active mode's notes show them.
   for base in leds['banks'].values():
    for i in range(8):want[(pad,base+i)]=want[(pad+1,base+i)]=on(row[18+i]) if base==bank else 0
   if len(states)>192+ch:want[(0xaf+ch,leds['meter'])]=min(127,round(states[192+ch]*127/11))
  for led,key in leds['common'].items():want[key]=on(states[led])
  out=bytearray()
  for (status,note),value in want.items():
   if self.led_sent.get((status,note))!=value:
    self.led_sent[(status,note)]=value;out+=bytes((status,note,value))
  return bytes(out)
 def led_reset(self):self.led_sent.clear()
 def led_off(self):
  out=bytes(v for (status,note),value in self.led_sent.items() if value for v in (status,note,0))
  self.led_sent.clear();return out
class LedState:
 """Poll the panel LED states published by the player shim (led-publish.c)."""
 MAGIC=0x4c335852
 def __init__(self,path):self.path=path;self.fd=None;self.seq=None;self.retry=0.
 def reset(self):self.seq=None
 def poll(self,now):
  if self.fd is None:
   if now<self.retry:return None
   try:self.fd=os.open(self.path,os.O_RDONLY)
   except OSError:self.retry=now+1;return None
  magic,seq=struct.unpack('<II',os.pread(self.fd,8,0).ljust(8,b'\0'))
  if magic!=self.MAGIC or seq==self.seq:return None
  states=os.pread(self.fd,195,8)
  if len(states)<195:return None
  self.seq=seq;return states
 def close(self):
  if self.fd is not None:os.close(self.fd);self.fd=None
def listen_reconnecting(b,lib,running,discover,sleep=time.sleep,leds=None,clock=time.monotonic):
 """Release controller gestures on loss; rediscover ALSA numbering on return."""
 buf=ctypes.create_string_buffer(1024)
 waiting=False
 while running():
  handle=ctypes.c_void_p();out=ctypes.c_void_p()
  try:
   devices=discover()
   if len(devices)!=1:raise OSError(errno.ENODEV,'Expected one controller MIDI input')
   rc=lib.snd_rawmidi_open(ctypes.byref(handle),ctypes.byref(out) if leds else None,devices[0].encode(),2)
   if rc<0:raise OSError(-rc,'Cannot open controller MIDI input')
  except (OSError,subprocess.SubprocessError) as e:
   if not waiting:print(f'Waiting for controller MIDI: {e}',flush=True)
   waiting=True
   for _ in range(20):
    if not running():break
    sleep(.05)
   continue
  waiting=False
  print(f'Listening to {devices[0]}'+(' (with LED output)' if leds else ' (input only)'),flush=True)
  # A new connection starts with unknown LEDs; resend every state.
  b.led_reset();pending=b''
  if leds:leds.reset()
  try:
   while running():
    n=lib.snd_rawmidi_read(handle,buf,len(buf))
    if n>0:b.feed(buf.raw[:n])
    elif n in (0,-errno.EAGAIN):sleep(.005)
    elif n!=-errno.EINTR:
     print(f'Controller MIDI read failed ({n}); reconnecting',flush=True)
     break
    b.tick()
    if leds:
     states=leds.poll(clock())
     if states:pending+=b.led_frame(states)
     if pending:
      w=lib.snd_rawmidi_write(out,pending,len(pending))
      if w>0:pending=pending[w:]
  finally:
   try:b.release()
   finally:
    lib.snd_rawmidi_close(handle)
    if leds and out.value:
     off=b.led_off()
     if off:lib.snd_rawmidi_write(out,off,len(off))
     lib.snd_rawmidi_close(out)
    # Never carry a partial message or old 14-bit MSB into a new connection.
    b.status=None;b.data=[];b.msb.clear()
  # Avoid a busy reconnect loop if ALSA still lists a failed device.
  for _ in range(20):
   if not running():break
   sleep(.05)
def main():
 # ./rx3 start passes every path; the defaults only suit a manual run.
 runtime=os.environ.get('RX3_RUNTIME')
 p=argparse.ArgumentParser();p.add_argument('--mapping',default=os.path.expanduser('~/.mixxx/controllers/Pioneer-DDJ-FLX6.midi.xml'))
 p.add_argument('--fifo',default=runtime and os.path.join(runtime,'dev/rx3-control'),help='runtime dev/rx3-control (default from RX3_RUNTIME)')
 p.add_argument('--player-id',help='launcher identity for this player session')
 p.add_argument('--state',help='jog counter file kept across bridge restarts')
 p.add_argument('--port-name',default='DDJ-FLX6',help='MIDI port name shown by amidi -l')
 p.add_argument('--model',choices=sorted(PROFILES),default='DDJ-FLX6',help='controller the mapping XML is for')
 p.add_argument('--led-state',help='LED states published by the player shim (default: next to --fifo)')
 p.add_argument('--replay');p.add_argument('--dry-run',action='store_true')
 p.add_argument('--check-mapping',action='store_true',help='only load the mapping and report the binding count');a=p.parse_args()
 if a.check_mapping:
  print(f'Loaded {len(Bridge(a.mapping,lambda *c:None,model=a.model).mapping)} MIDI bindings from {a.mapping}');return
 if not a.dry_run and not a.fifo:p.error('--fifo is required (or set RX3_RUNTIME)')
 fd=None if a.dry_run else os.open(a.fifo,os.O_RDWR|os.O_NONBLOCK)
 def emit(*cmd):
  if fd is not None:os.write(fd,struct.pack('<iiiifi',*cmd))
  if a.replay or a.dry_run:print(cmd,flush=True)
 b=Bridge(a.mapping,emit,model=a.model);print(f'Loaded {len(b.mapping)} MIDI bindings from {a.mapping}',flush=True)
 if a.replay:
  b.feed(open(a.replay,'rb').read());b.release();return
 # Preserve absolute jog counters across MIDI-reader restarts in this player session.
 player=a.player_id or subprocess.check_output(['pgrep','-x','rbp-pi'],text=True).strip()
 statefile=a.state or os.path.join(os.path.dirname(a.fifo or '.'),'rx3-midi-jog-state.json')
 try:
  previous=json.load(open(statefile))
  if previous.get('player')==player:
   for ch in (1,2):b.jogs[ch]['total']=int(previous['totals'][str(ch)])
 except (OSError,ValueError,KeyError,TypeError):pass
 lib=ctypes.CDLL('libasound.so.2');handle=ctypes.c_void_p()
 lib.snd_rawmidi_open.argtypes=[ctypes.POINTER(ctypes.c_void_p),ctypes.POINTER(ctypes.c_void_p),ctypes.c_char_p,ctypes.c_int]
 lib.snd_rawmidi_read.argtypes=[ctypes.c_void_p,ctypes.c_void_p,ctypes.c_size_t];lib.snd_rawmidi_read.restype=ctypes.c_ssize_t
 lib.snd_rawmidi_write.argtypes=[ctypes.c_void_p,ctypes.c_char_p,ctypes.c_size_t];lib.snd_rawmidi_write.restype=ctypes.c_ssize_t
 lib.snd_rawmidi_close.argtypes=[ctypes.c_void_p]
 led_path=a.led_state or (a.fifo and os.path.join(os.path.dirname(a.fifo),'rx3-led-state'))
 leds=LedState(led_path) if b.leds and led_path else None
 def discover():
  listing=subprocess.check_output(['amidi','-l'],text=True)
  return re.findall(r'^I[O ]\s+(hw:\S+)\s+.*'+re.escape(a.port_name),listing,re.M)
 running=True
 def stop(*_):
  nonlocal running
  running=False
 signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
 try:
  listen_reconnecting(b,lib,lambda:running,discover,leds=leds)
 finally:
  b.release()
  if leds:leds.close()
  with open(statefile+'.tmp','w') as f:json.dump({'player':player,'totals':{ch:j['total'] for ch,j in b.jogs.items()}},f)
  os.replace(statefile+'.tmp',statefile)
  if fd is not None:os.close(fd)
if __name__=='__main__':main()
