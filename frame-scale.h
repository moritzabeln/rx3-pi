#ifndef RX3_FRAME_SCALE_H
#define RX3_FRAME_SCALE_H
#include <stdint.h>
#include <string.h>
/* Pixel-exact equivalent of 1280x800 -> nearest 1920x1200 -> rotate.
 * Rotate at native size, then write complete scanout rows. The reversed X
 * axis uses ceil(2*x/3), preserving the old scaler's orientation and phase. */
static void rx3_fullscreen_present(unsigned char *dst,unsigned pitch,
 const uint32_t *src,uint32_t *rotated){
 for(int by=0;by<1280;by+=16)for(int bx=0;bx<800;bx+=16)
  for(int y=by;y<by+16;y++)for(int x=bx;x<bx+16;x++)
   rotated[y*800+x]=src[(799-x)*1280+y];
 uint32_t row[1200];int previous=-1;
 for(int y=0;y<1920;y++){
  int sy=y*2/3;
  if(sy!=previous){
   const uint32_t *in=rotated+sy*800;
   for(int x=0;x<1200;x+=3){int sx=x*2/3;row[x]=in[sx];row[x+1]=row[x+2]=in[sx+1];}
   previous=sy;
  }
  memcpy(dst+y*pitch,row,sizeof(row));
 }
}
/* 1280x800 -> 960x600 between 32-px black bars; 4->3 area average so thin lines don't vanish.
 * Two 8-bit channels share each 32-bit word; the largest lane sum is 255*16. */
static void rx3_letterbox_present(unsigned char *dst,unsigned pitch,const uint32_t *src){
 uint32_t rb[1280],ag[1280];
 for(int y=0;y<600;y++){
  int r=y%3;uint32_t w0=3-r,w1=1+r;
  const uint32_t *a=src+(y/3*4+r)*1280,*b=a+1280;
  for(int x=0;x<1280;x++){
   rb[x]=(a[x]&0xff00ff)*w0+(b[x]&0xff00ff)*w1;
   ag[x]=(a[x]>>8&0xff00ff)*w0+(b[x]>>8&0xff00ff)*w1;
  }
  uint32_t *row=(uint32_t*)(dst+y*pitch);
  memset(row,0,32*4);memset(row+992,0,32*4);
  for(int x=0;x<960;x++){
   int q=x%3,sx=x/3*4+q;uint32_t v0=3-q,v1=1+q;
   uint32_t lo=(rb[sx]*v0+rb[sx+1]*v1+0x80008)>>4&0xff00ff;
   uint32_t hi=(ag[sx]*v0+ag[sx+1]*v1+0x80008)>>4&0xff00ff;
   row[32+x]=lo|hi<<8;
  }
 }
}
#endif
