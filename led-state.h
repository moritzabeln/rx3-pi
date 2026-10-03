/* Read one LED from the RX3 1.19 player's uif::LedStat, as MainGpioLed::controGpio does.
 * LedStat: +4 u16 record count, +8 44-byte Led records, +14 u16 channel stride,
 * +16 u16 table[id*stride + channel-1] = 1-based record index (0: no LED).
 * Led: +8 linked id, +12 linked channel, +16 state (1 on, 2 blink, 3 follow a blinking LED),
 * +20 brightness (0 full, 1 dim: e.g. an empty hot cue), +28 blink period (ms),
 * +32 blink start (ms), +36 level meter bits (bar from bit 0, plus a peak-hold bit). */
#ifndef RX3_LED_STATE_H
#define RX3_LED_STATE_H
#include <stdint.h>
#define RX3_LED_MAX 54
#define RX3_LED_LIT 1
#define RX3_LED_ACTIVE 2
#define RX3_LED_DIM 4
#define RX3_LED_BLINK 8
#define RX3_LED_LEVEL_METER 54
/* Player pointers are 32-bit; host tests set a base for their arena. */
#ifndef RX3_LED_BASE
#define RX3_LED_BASE 0
#endif
static const void *rx3_led_ptr(const uint8_t *field){
 uint32_t address=*(const uint32_t*)field;
 return address?(const void*)((uintptr_t)RX3_LED_BASE+address):0;
}
static const uint8_t *rx3_led_record(const uint8_t *ls,uint32_t id,uint32_t ch){
 uint32_t stride=*(const uint16_t*)(ls+14),count=*(const uint16_t*)(ls+4);
 const uint16_t *table=rx3_led_ptr(ls+16);
 const uint8_t *records=rx3_led_ptr(ls+8);
 if(!table||!records||!stride||id>RX3_LED_MAX||ch>stride)return 0;
 uint32_t n=table[id*stride+(ch?ch-1:0)];
 if(!n||n>count)return 0;
 return records+44*(n-1);
}
static int rx3_led_blink_on(const uint8_t *r,long long now){
 uint32_t period=*(const uint32_t*)(r+28),start=*(const uint32_t*)(r+32);
 return !period||(((unsigned long long)(now-(long long)start)/period)&1)==0;
}
/* RX3_LED_ACTIVE: lit or blinking; RX3_LED_LIT: lit at this moment; RX3_LED_DIM: reduced brightness;
 * RX3_LED_BLINK: blinking rather than steady (e.g. Beat FX ON/OFF while the effect is on). */
static uint8_t rx3_led_value(const uint8_t *ls,uint32_t id,uint32_t ch,long long now){
 const uint8_t *r=rx3_led_record(ls,id,ch);
 if(!r)return 0;
 uint32_t state=*(const uint32_t*)(r+16);
 uint8_t dim=*(const uint32_t*)(r+20)?RX3_LED_DIM:0;
 if(state==1)return RX3_LED_ACTIVE|RX3_LED_LIT|dim;
 if(state==3)r=rx3_led_record(ls,*(const uint32_t*)(r+8),*(const uint32_t*)(r+12));
 if(r&&*(const uint32_t*)(r+16)==2)return RX3_LED_ACTIVE|RX3_LED_BLINK|(rx3_led_blink_on(r,now)?RX3_LED_LIT:0)|dim;
 return 0;
}
/* Lit segments of a level meter bar (the RX3 has 11, -24 to +14 dB); the peak-hold bit is ignored. */
static uint8_t rx3_led_level(const uint8_t *ls,uint32_t ch){
 const uint8_t *r=rx3_led_record(ls,RX3_LED_LEVEL_METER,ch);
 uint32_t bits=r?*(const uint32_t*)(r+36):0;uint8_t n=0;
 while(n<32&&(bits>>n&1))n++;
 return n;
}
#endif
