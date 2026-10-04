#!/usr/bin/env python3
"""Backspin: a fast spin keeps native jog touch held after the controller's touch release."""
import importlib.util,tempfile
from pathlib import Path
s=importlib.util.spec_from_file_location('bridge',Path(__file__).with_name('flx6-rx3.py'));m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
TOUCH=(0x4306,)
with tempfile.NamedTemporaryFile(mode='w') as f:
 controls=''.join(f'<control><group>[Channel{d}]</group><key>PioneerDDJFLX6.{k}</key><status>{st+d}</status><midino>{n}</midino></control>'
  for d in (1,2) for k,st,n in (('shiftPressed',0x8f,0x3f),('jogTouch',0x8f,0x36),('jogTurn',0xaf,0x21)))
 f.write('<root><controls>'+controls+'</controls></root>');f.flush()
 def setup():
  events=[];clock=[0.];b=m.Bridge(f.name,lambda *e:events.append(e),lambda:clock[0]);return events,clock,b
 def touch(b,d,down):b.feed(bytes([0x8f+d,0x36,127 if down else 0]))
 def spin(b,clock,d,value,steps,gap=.01):
  for _ in range(steps):b.feed(bytes([0xaf+d,0x21,value])*3);clock[0]+=gap;b.tick()
 releases=lambda events:[e for e in events if e[:3]==(0x4306,2,1)]
 # Fast spin (backward) then release: touch stays held until the wheel stops.
 events,clock,b=setup();touch(b,1,True);spin(b,clock,1,1,20)
 assert b.recent_speed(1)<-m.COAST_START_SPEED
 touch(b,1,False);assert not releases(events) and b.jogs[1]['coast'] is not None
 spin(b,clock,1,30,10);assert not releases(events)  # coasting ticks still reach the player
 assert events[-1][0]==0x4305 and events[-1][4]<0
 clock[0]+=.2;b.tick();assert len(releases(events))==1 and b.jogs[1]['coast'] is None and not b.held
 assert events[-2][:5]==(0x4305,4,1,0,0.)
 # Forward spin works the same way.
 events,clock,b=setup();touch(b,1,True);spin(b,clock,1,127,20);touch(b,1,False)
 assert b.jogs[1]['coast'] is not None and not releases(events)
 # A slow release is immediate.
 events,clock,b=setup();touch(b,1,True);spin(b,clock,1,70,20);touch(b,1,False)
 assert len(releases(events))==1 and b.jogs[1]['coast'] is None
 # The coast ends after three seconds even if the wheel keeps spinning.
 events,clock,b=setup();touch(b,1,True);spin(b,clock,1,1,20);touch(b,1,False)
 spin(b,clock,1,1,int(m.COAST_MAX/.01)-5);assert not releases(events)
 spin(b,clock,1,1,10);assert len(releases(events))==1
 # A new touch takes over without a second native press; its release ends normally.
 events,clock,b=setup();touch(b,1,True);spin(b,clock,1,1,20);touch(b,1,False);n=len(events)
 touch(b,1,True);assert len(events)==n and b.jogs[1]['coast'] is None and (TOUCH[0],1) in b.held
 clock[0]+=.2;touch(b,1,False);assert len(releases(events))==1
 # Shift cancels the coast and releases the native touch; the other deck is unaffected.
 events,clock,b=setup();touch(b,1,True);spin(b,clock,1,1,20);touch(b,1,False)
 touch(b,2,True);assert b.jogs[2]['coast'] is None
 b.feed(bytes([0x90,0x3f,127]));assert b.jogs[1]['coast'] is None and len(releases(events))==1
 # Controller loss releases a coasting touch.
 events,clock,b=setup();touch(b,1,True);spin(b,clock,1,1,20);touch(b,1,False);b.release()
 assert len(releases(events))==1 and not b.held and b.jogs[1]['coast'] is None
print('PASS: fast spins coast with jog touch held; slow release, timeout, re-touch, shift and loss end it')
