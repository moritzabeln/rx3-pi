#include <assert.h>
#include <stdlib.h>
#include <stdio.h>
#include <string.h>
#include <time.h>
#include "frame-scale.h"
#ifdef RX3_SCALE_DRM
#include <fcntl.h>
#include <unistd.h>
#include <sys/mman.h>
#include <xf86drm.h>
#include <drm_mode.h>
#endif
/* Per-channel area average, independent of the packed-lane implementation. */
static uint32_t letterbox_reference(const uint32_t *src,int x,int y){
 int r=y%3,q=x%3,sy=y/3*4+r,sx=x/3*4+q,wy[2]={3-r,1+r},wx[2]={3-q,1+q};uint32_t out=0;
 for(int c=0;c<32;c+=8){unsigned s=0;
  for(int j=0;j<2;j++)for(int i=0;i<2;i++)s+=wy[j]*wx[i]*(src[(sy+j)*1280+sx+i]>>c&255);
  out|=(uint32_t)((s+8)>>4)<<c;}
 return out;
}
static void test_letterbox(uint32_t *src){
 for(unsigned pad=0;pad<=64;pad+=64){
  unsigned pitch=4096+pad;unsigned char *dst=malloc(pitch*600);assert(dst);memset(dst,0xa5,pitch*600);
  struct timespec a,b;clock_gettime(CLOCK_MONOTONIC,&a);
  for(int i=0;i<20;i++)rx3_letterbox_present(dst,pitch,src);
  clock_gettime(CLOCK_MONOTONIC,&b);
  for(int y=0;y<600;y++){
   const uint32_t *row=(void*)(dst+y*pitch);
   for(int x=0;x<32;x++)assert(row[x]==0&&row[992+x]==0);
   for(int x=0;x<960;x++)assert(row[32+x]==letterbox_reference(src,x,y));
   for(unsigned x=4096;x<pitch;x++)assert(dst[y*pitch+x]==0xa5);
  }
  printf("PASS exact letterbox 960x600 in 1024x600, pitch %u, %.3f ms/frame\n",pitch,((b.tv_sec-a.tv_sec)*1e3+(b.tv_nsec-a.tv_nsec)/1e6)/20);
  free(dst);
 }
 /* A 1-pixel white line at any phase of the 4->3 pattern keeps at least half its brightness. */
 unsigned char *dst=malloc(4096*600);assert(dst);
 for(int x0=0;x0<4;x0++){
  for(unsigned i=0;i<1280*800;i++)src[i]=i%1280==(unsigned)(640+x0)?0xffffff:0;
  rx3_letterbox_present(dst,4096,src);
  unsigned best=0;const uint32_t *row=(void*)(dst+300*4096);
  for(int x=0;x<1024;x++)if((row[x]&255)>best)best=row[x]&255;
  assert(best>=127);
 }
 printf("PASS 1-pixel lines survive the letterbox scale\n");free(dst);
}
int main(void){
 uint32_t *src=malloc(1280*800*4),*rotated=malloc(1280*800*4);assert(src&&rotated);
 for(unsigned i=0;i<1280*800;i++)src[i]=i*2654435761u;
 for(unsigned pad=0;pad<=64;pad+=64){
  unsigned pitch=4800+pad;unsigned char *dst;
#ifdef RX3_SCALE_DRM
  if(pad)break;
  int fd=open("/dev/dri/card0",O_RDWR);assert(fd>=0);
  struct drm_mode_create_dumb create={.width=1200,.height=1920,.bpp=32};
  assert(drmIoctl(fd,DRM_IOCTL_MODE_CREATE_DUMB,&create)==0);pitch=create.pitch;
  struct drm_mode_map_dumb map={.handle=create.handle};assert(drmIoctl(fd,DRM_IOCTL_MODE_MAP_DUMB,&map)==0);
  dst=mmap(0,create.size,PROT_READ|PROT_WRITE,MAP_SHARED,fd,map.offset);assert(dst!=MAP_FAILED);
#else
  dst=malloc(pitch*1920);assert(dst);
#endif
  memset(dst,0xa5,pitch*1920);struct timespec a,b;clock_gettime(CLOCK_MONOTONIC,&a);
  for(int i=0;i<20;i++)rx3_fullscreen_present(dst,pitch,src,rotated);
  clock_gettime(CLOCK_MONOTONIC,&b);
  for(int y=0;y<1920;y++){
   const uint32_t *row=(void*)(dst+y*pitch);
   for(int x=0;x<1200;x++)assert(row[x]==src[((1199-x)*2/3)*1280+y*2/3]);
   for(unsigned x=4800;x<pitch;x++)assert(dst[y*pitch+x]==0xa5);
  }
  printf("PASS exact fullscreen scale+rotate, pitch %u, %.3f ms/frame\n",pitch,((b.tv_sec-a.tv_sec)*1e3+(b.tv_nsec-a.tv_nsec)/1e6)/20);
#ifdef RX3_SCALE_DRM
  munmap(dst,create.size);struct drm_mode_destroy_dumb destroy={.handle=create.handle};
  assert(drmIoctl(fd,DRM_IOCTL_MODE_DESTROY_DUMB,&destroy)==0);close(fd);
#else
  free(dst);
#endif
 }
 test_letterbox(src);
 free(src);free(rotated);return 0;
}
