#ifndef RX3_NATIVE_SCREEN_H
#define RX3_NATIVE_SCREEN_H
#include <stdint.h>
/* Main deck-information panel (native window key136) exists before loading
 * a track too. Its visibility distinguishes the player from Browse. */
static inline int player_screen_active(void){
 void *active=*(void *volatile *)(0x024942bc+0x20);
 return active&&*(volatile int16_t*)((char*)active+6)==0;
}
static inline int main_panel_visible(void){
 if(!player_screen_active())return 0;
 uintptr_t p=*(volatile uint32_t*)0x0245b880;
 for(int i=0;p&&i<256;i++,p=*(volatile uint32_t*)(p+0x84)){
  if(*(volatile uint32_t*)(p+0xc)==136)return (*(volatile uint32_t*)(p+4)&8)!=0;
 }
 return 0;
}
/* EJECT sits above the native MY SETTINGS MENU button (756,532) of the Source info pane. */
#define RX3_SOURCE_EJECT_X 756
#define RX3_SOURCE_EJECT_Y 452
#define RX3_SOURCE_EJECT_W 504
#define RX3_SOURCE_EJECT_H 60
/* Source view (browse mode 12): USB slot of the highlighted row, or 0. The rows list
 * the connected-media bits in order; bits 1/2 are media 2/3 (USB1/USB2), bit 0 PC, bit 3 LINK. */
static inline int source_eject_slot(void){
 if(!player_screen_active()||main_panel_visible()||((int(*)(void))0x1126d0)()!=12)return 0;
 unsigned media=*(volatile uint8_t*)0x0326f8b4;int row=((int(*)(int))0x1127e4)(0);
 for(int bit=0;bit<4;bit++)if((media>>bit&1)&&row--==0)return bit==1||bit==2?bit:0;
 return 0;
}
#endif
