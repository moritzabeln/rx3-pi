"""DDJ-400 bytes from Pioneer's official MIDI list, through a Mixxx-style mapping."""
import importlib.util, pathlib, tempfile, unittest
spec=importlib.util.spec_from_file_location('bridge',pathlib.Path(__file__).with_name('flx6-rx3.py'))
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
CONTROLS=[('[Library]','MoveVertical',0xb6,0x40,''),('[Library]','MoveFocusForward',0x96,0x41,''),
 ('[Library]','MoveFocusBackward',0x96,0x42,''),('[Channel1]','LoadSelectedTrack',0x96,0x46,''),
 ('[Channel2]','LoadSelectedTrack',0x96,0x47,'')]
for deck in (1,2):
 n=0x8f+deck;cc=0xaf+deck;pads=0x95+2*deck
 CONTROLS+=[(f'[Channel{deck}]',k,s,no,o) for k,s,no,o in (
  ('PioneerDDJ400.syncPressed',n,0x58,''),('PioneerDDJ400.syncLongPressed',n,0x5c,''),
  ('PioneerDDJ400.shiftPressed',n,0x3f,''),('PioneerDDJ400.jogTouch',n,0x36,''),
  ('PioneerDDJ400.jogTurn',cc,0x22,''),('PioneerDDJ400.jogSearch',cc,0x29,''),
  ('PioneerDDJ400.tempoSliderMSB',cc,0x00,''),('PioneerDDJ400.tempoSliderLSB',cc,0x20,''),
  ('PioneerDDJ400.cueLoopCallLeft',n,0x51,''),('PioneerDDJ400.cueLoopCallRight',n,0x53,''),
  ('PioneerDDJ400.quickJumpBack',n,0x3e,''),('PioneerDDJ400.quickJumpForward',n,0x3d,''),
  ('volume',cc,0x13,'<fourteen-bit-msb/>'),('volume',cc,0x33,'<fourteen-bit-lsb/>'),
  ('hotcue_1_activate',pads,0x00,''),('beatloop_4_toggle',pads,0x64,''),
  ('PioneerDDJ400.beatjumpPadPressed',pads,0x21,''))]
XML='<root><controls>'+''.join(f'<control><group>{g}</group><key>{k}</key><status>{s:#x}</status><midino>{n:#x}</midino><options>{o}</options></control>' for g,k,s,n,o in CONTROLS)+'</controls></root>'
class DDJ400(unittest.TestCase):
 def setUp(self):
  self.f=tempfile.NamedTemporaryFile(mode='w',suffix='.xml');self.f.write(XML);self.f.flush()
  self.events=[];self.clock=[0.]
  self.b=m.Bridge(self.f.name,lambda *e:self.events.append(e),lambda:self.clock[0],model='DDJ-400')
 def tearDown(self):self.f.close()
 def feed(self,*data):self.events.clear();self.b.feed(bytes(data));return self.events
 def test_browser(self):
  # Clockwise 01-1E, counter-clockwise 7F-62.
  self.assertEqual(self.feed(0xb6,0x40,0x1e,0x40,0x62),[(0x420c,4,0,30,0.,0x4252),(0x420c,4,0,-30,0.,0x4252)])
  # Clockwise zooms in on the DDJ-400 (confirmed on hardware), opposite to the FLX6 sign.
  self.assertEqual(self.feed(0xb6,0x64,0x1e,0x64,0x62,0x64,0x7f),
   [(0x420c,4,0,1,0.,0x425a),(0x420c,4,0,-1,0.,0x425a),(0x420c,4,0,-1,0.,0x425a)])
  self.assertEqual(self.feed(0x96,0x41,0x7f,0x41,0),[(0x420c,0,0,0,0.,0x4250)])
  self.assertEqual(self.feed(0x96,0x42,0x7f,0x42,0),[(0x420d,0,0,0,0.,0x424b)])
  self.assertEqual(self.feed(0x96,0x46,0x7f,0x47,0x7f),[(0x4311,0,1,0,0.,0),(0x4311,0,2,0,0.,0)])
  # Shift + LOAD deck 2 toggles player/library with the native Browse key.
  self.assertEqual(self.feed(0x96,0x7a,0x7f,0x7a,0),[(0x202,0,0,0,0.,0),(0x202,2,0,0,0.,0)])
 def test_cue_loop_call(self):
  self.assertEqual(self.feed(0x90,0x51,0x7f,0x51,0),[(0x4323,0,1,0,0.,0),(0x4323,2,1,0,0.,0)])
  self.assertEqual(self.feed(0x91,0x53,0x7f),[(0x4322,0,2,0,0.,0)])
  self.assertEqual(self.feed(0x90,0x3e,0x7f),[(0x4124,0,1,0,0.,0)])
  self.assertEqual(self.feed(0x91,0x3d,0x7f),[(0x4125,0,2,0,0.,0)])
 def test_sync_short_and_long(self):
  self.assertEqual(self.feed(0x90,0x58,0x7f,0x58,0),[(0x4112,0,1,0,0.,0),(0x4112,2,1,0,0.,0)])
  self.assertEqual(self.feed(0x91,0x5c,0x7f,0x5c,0),[(0x4111,0,2,0,0.,0),(0x4111,2,2,0,0.,0)])
 def test_tempo_minus_side_is_minimum(self):
  self.assertEqual(self.feed(0xb0,0x00,0,0x20,0),[(0x4109,5,1,0,-1.,0x544d)])
  self.assertEqual(self.feed(0xb1,0x00,0x40,0x20,0),[(0x4109,5,2,0,0.,0x544d)])
  self.assertAlmostEqual(self.feed(0xb1,0x00,0x7f,0x20,0x7f)[0][4],1.,places=3)
 def test_jog_720_ticks_is_one_revolution(self):
  self.b.feed(bytes([0x90,0x36,0x7f]))
  self.b.feed(bytes([0xb0,0x22,0x41]*720));self.clock[0]+=1.8;self.b.tick()
  key,op,ch,_,speed,pulse=self.events[-1]
  self.assertEqual((key,op,ch,pulse),(0x4305,4,1,1620))
  self.assertAlmostEqual(speed,1.)
 def test_shift_jog_grid_step(self):
  self.b.feed(bytes([0x90,0x3f,0x7f]))
  self.assertEqual(self.feed(0xb0,0x29,0x41),[])
  self.assertEqual(self.feed(0xb0,0x29,0x41),[(0,4,1,20,0.,0x4744)])
 def test_pads_and_fader(self):
  self.assertEqual(self.feed(0x97,0x00,0x7f)[-1],(0x4117,0,1,0,0.,0x5040))
  self.assertEqual(self.feed(0x99,0x64,0x7f)[-1],(0x411b,0,2,0,0.,0x5041))
  self.assertEqual(self.feed(0x99,0x21,0x7f)[-1],(0x4118,0,2,0,0.,0x5043))
  self.assertEqual(self.feed(0xb0,0x13,0x7f,0x33,0x7f),[(0x501e,4,1,0,1.,0)])
class Leds(unittest.TestCase):
 def setUp(self):
  self.f=tempfile.NamedTemporaryFile(mode='w',suffix='.xml');self.f.write(XML);self.f.flush()
  self.b=m.Bridge(self.f.name,lambda *e:None,model='DDJ-400')
 def tearDown(self):self.f.close()
 def frame(self,**lit):
  states=bytearray(192)
  for name,value in lit.items():ch,led=name[1:].split('_');states[int(ch)*64+int(led)]=value
  return self.b.led_frame(bytes(states))
 @staticmethod
 def messages(data):return {(data[i],data[i+1],data[i+2]) for i in range(0,len(data),3)}
 def test_first_frame_sets_every_led_then_only_changes(self):
  # 13 deck notes x 2 decks, 3 pad modes x 8 pads x 2 layers x 2 decks, master cue.
  self.assertEqual(len(self.frame())//3,26+96+1)
  self.assertEqual(self.frame(),b'')
  self.assertEqual(self.messages(self.frame(c1_1=3)),{(0x90,0x0b,0x7f),(0x90,0x47,0x7f)})
  self.assertEqual(self.messages(self.frame(c1_1=3,c0_51=3)),{(0x96,0x63,0x7f)})
  # Blinking: active without lit turns the LED off until the next lit phase.
  self.assertEqual(self.messages(self.frame(c1_1=2,c0_51=3)),{(0x90,0x0b,0),(0x90,0x47,0)})
 def test_pads_follow_the_active_rx3_pad_mode(self):
  self.frame()
  self.assertEqual(self.messages(self.frame(c2_14=3,c2_18=3)),{(0x99,0x00,0x7f),(0x9a,0x00,0x7f)})
  self.assertEqual(self.messages(self.frame(c2_17=3,c2_18=3)),
   {(0x99,0x00,0),(0x9a,0x00,0),(0x99,0x20,0x7f),(0x9a,0x20,0x7f)})
  # A blinking mode LED still selects the bank.
  self.assertEqual(self.frame(c2_17=2,c2_18=3),b'')
 def test_off_and_reset(self):
  self.frame(c1_2=3)
  self.assertEqual(self.messages(self.b.led_off()),{(0x90,0x0c,0),(0x90,0x48,0)})
  self.assertEqual(len(self.frame())//3,123)
 def test_flx6_has_no_led_output(self):
  b=m.Bridge(self.f.name,lambda *e:None);self.assertIsNone(b.leds);self.assertEqual(b.led_frame(bytes(192)),b'')
 def test_state_file_reader(self):
  import struct
  with tempfile.TemporaryDirectory() as d:
   path=pathlib.Path(d)/'rx3-led-state';r=m.LedState(str(path))
   self.assertIsNone(r.poll(0.))
   path.write_bytes(struct.pack('<II',m.LedState.MAGIC,1)+bytes(range(192)))
   self.assertIsNone(r.poll(.5))                      # retry waits a second
   self.assertEqual(r.poll(1.),bytes(range(192)))
   self.assertIsNone(r.poll(1.))                      # unchanged sequence
   r.reset();self.assertEqual(r.poll(1.),bytes(range(192)))
   with open(path,'r+b') as f:f.seek(4);f.write(struct.pack('<I',2))
   self.assertEqual(r.poll(1.),bytes(range(192)));r.close()
 def test_reconnect_loop_writes_leds_and_turns_them_off(self):
  import ctypes,errno
  class Source:
   polls=0
   def reset(self):pass
   def poll(self,now):
    self.polls+=1;s=bytearray(192);s[64+1]=3;return bytes(s) if self.polls==1 else None
  class Alsa:
   written=b'';closes=0;reads=0
   def snd_rawmidi_open(self,handle,output,device,mode):
    ctypes.cast(output,ctypes.POINTER(ctypes.c_void_p))[0]=0x1234;return 0
   def snd_rawmidi_read(self,handle,buf,size):
    self.reads+=1;return -errno.EAGAIN
   def snd_rawmidi_write(self,out,data,size):self.written+=bytes(data[:size]);return size
   def snd_rawmidi_close(self,handle):self.closes+=1
  alsa=Alsa();reads=iter(range(3))
  m.listen_reconnecting(self.b,alsa,lambda:next(reads,None) is not None,lambda:['hw:1,0,0'],lambda _:None,leds=Source())
  sent=self.messages(alsa.written)
  self.assertIn((0x90,0x0b,0x7f),sent);self.assertIn((0x90,0x0b,0),sent)
  self.assertEqual(alsa.written[-6:],bytes((0x90,0x0b,0,0x90,0x47,0)))
  self.assertEqual(alsa.closes,2)
if __name__=='__main__':unittest.main()
