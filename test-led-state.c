#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
static uint8_t arena[4096];
#define RX3_LED_BASE ((uintptr_t)arena)
#include "led-state.h"
/* Arena layout mirrors the player's 32-bit LedStat: object at 0, table at 256, records at 1024. */
enum{TABLE=256,RECORDS=1024,STRIDE=2};
static void put32(uint32_t at,uint32_t v){memcpy(arena+at,&v,4);}
static void put16(uint32_t at,uint16_t v){memcpy(arena+at,&v,2);}
static uint16_t count;
static void led(uint32_t id,uint32_t ch,uint32_t state,uint32_t period,uint32_t start){
 uint32_t r=RECORDS+44*count++;
 put16(TABLE+2*(id*STRIDE+ch-1),count);put16(4,count);
 put32(r+16,state);put32(r+28,period);put32(r+32,start);
}
int main(void){
 put32(8,RECORDS);put16(14,STRIDE);put32(16,TABLE);
 led(1,1,1,0,0);                 /* deck1 Play: on */
 led(1,2,2,500,1000);            /* deck2 Play: blinking 500 ms from t=1000 */
 led(2,1,0,0,0);                 /* deck1 Cue: off */
 led(4,2,3,0,0);                 /* deck2 Sync: follows deck2 Play */
 put32(RECORDS+44*3+8,1);put32(RECORDS+44*3+12,2);
 led(7,1,3,0,0);                 /* deck1 LoopIn: follows steady deck1 Play, so dark */
 put32(RECORDS+44*4+8,1);put32(RECORDS+44*4+12,1);
 led(18,1,1,0,0);put32(RECORDS+44*5+20,1);     /* deck1 Pad1: empty hot cue, on but dim */
 led(54,1,1,0,0);put32(RECORDS+44*6+36,0x87);  /* deck1 meter: 3-segment bar + peak bit */
 const uint8_t *ls=arena;
 assert(rx3_led_value(ls,18,1,0)==(RX3_LED_ACTIVE|RX3_LED_LIT|RX3_LED_DIM));
 assert(rx3_led_level(ls,1)==3);
 assert(rx3_led_level(ls,2)==0);
 assert(rx3_led_value(ls,1,1,0)==(RX3_LED_ACTIVE|RX3_LED_LIT));
 assert(rx3_led_value(ls,1,0,0)==(RX3_LED_ACTIVE|RX3_LED_LIT));
 assert(rx3_led_value(ls,1,2,1200)==(RX3_LED_ACTIVE|RX3_LED_LIT));
 assert(rx3_led_value(ls,1,2,1600)==RX3_LED_ACTIVE);
 assert(rx3_led_value(ls,1,2,2100)==(RX3_LED_ACTIVE|RX3_LED_LIT));
 assert(rx3_led_value(ls,4,2,1600)==RX3_LED_ACTIVE);
 assert(rx3_led_value(ls,4,2,2100)==(RX3_LED_ACTIVE|RX3_LED_LIT));
 assert(rx3_led_value(ls,7,1,0)==0);
 assert(rx3_led_value(ls,2,1,0)==0);
 assert(rx3_led_value(ls,3,1,0)==0);             /* no record */
 assert(rx3_led_value(ls,1,3,0)==0);             /* channel beyond stride */
 assert(rx3_led_value(ls,RX3_LED_MAX+1,1,0)==0);
 put16(4,2);assert(rx3_led_value(ls,4,2,2100)==0); /* index beyond record count */
 puts("PASS LedStat on/off/blink/follow/dim/meter decoding and bounds");
}
