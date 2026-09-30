/* Publish the player's panel LED states for controller LEDs (read by the MIDI bridge).
 * PanelComController::sendPanelConData() runs every 20 ms on the panel timer thread with
 * the complete LedStat that the XDJ-RX3 would send to its panel microcontrollers. */
#include <stdint.h>
#include <string.h>
#include <sys/mman.h>
#include <fcntl.h>
#include <unistd.h>
#include "led-state.h"
extern char *program_invocation_short_name;
#define MAGIC 0x4c335852u /* "RX3L": u32 magic, u32 sequence, u8 state[3][64] */
#define IDS 64
static uint8_t *shared,last[3*IDS];
static int announced;
static void (*original_send)(void*,const void*);
static void send_hook(void *self,const void *ledstat){
 long long now=((long long(*)(void))0x3a127c)(); /* juce::Time::currentTimeMillis */
 uint8_t current[3*IDS]={0};
 for(uint32_t ch=0;ch<3;ch++)for(uint32_t id=1;id<=RX3_LED_MAX;id++)
  current[ch*IDS+id]=rx3_led_value(ledstat,id,ch,now);
 if(memcmp(current,last,sizeof(current))){
  memcpy(last,current,sizeof(current));memcpy(shared+8,current,sizeof(current));
  __atomic_add_fetch((uint32_t*)shared+1,1,__ATOMIC_RELEASE);
 }
 if(!announced){announced=1;write(2,"LED state publisher active\n",27);}
 original_send(self,ledstat);
}
__attribute__((constructor))static void install_led_publisher(void){
 if(!program_invocation_short_name||strcmp(program_invocation_short_name,"rbp-pi"))return;
 uint32_t *entry=(void*)0x2dd56c;
 if(entry[0]!=0xe5903088||entry[1]!=0xe92d41f0)return;
 int out=open("/dev/rx3-led-state",O_RDWR|O_CREAT,0600);
 if(out<0)return;
 if(ftruncate(out,4096)){close(out);return;}
 shared=mmap(0,4096,PROT_READ|PROT_WRITE,MAP_SHARED,out,0);
 close(out);
 if(shared==MAP_FAILED){shared=0;return;}
 memset(shared+8,0,3*IDS);
 __atomic_store_n((uint32_t*)shared,MAGIC,__ATOMIC_RELEASE);
 long page=getpagesize();uint32_t *tr=mmap(0,page,PROT_READ|PROT_WRITE,MAP_PRIVATE|MAP_ANONYMOUS,-1,0);
 if(tr==MAP_FAILED)return;
 tr[0]=entry[0];tr[1]=entry[1];tr[2]=0xe51ff004;tr[3]=0x2dd574;
 if(mprotect(tr,page,PROT_READ|PROT_EXEC))return;
 syscall(0xf0002,tr,tr+4,0);original_send=(void*)tr;
 void *base=(void*)((uintptr_t)entry&~((uintptr_t)page-1));
 if(mprotect(base,page,PROT_READ|PROT_WRITE|PROT_EXEC))return;
 entry[0]=0xe51ff004;entry[1]=(uintptr_t)send_hook;
 syscall(0xf0002,entry,entry+2,0);mprotect(base,page,PROT_READ|PROT_EXEC);
 write(2,"LED state publisher installed\n",30);
}
