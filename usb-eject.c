/* USB STOP: the player detaches its database and calls umount()/umount2() on its slot path.
 * It runs unprivileged in the chroot, so pass /media/usb* unmounts to the host USB helper
 * (./rx3 usb-watch) through dev/rx3-usb-eject. Without the helper, fall back to libc. */
#include <errno.h>
#include <fcntl.h>
#include <string.h>
#include <unistd.h>
extern void *dlsym(void*,const char*);
static int request_eject(const char *target){
 if(!target||strncmp(target,"/media/usb",10))return -1;
 int fd=open("/dev/rx3-usb-eject",O_WRONLY|O_NONBLOCK|O_CLOEXEC);
 if(fd<0)return -1;
 char line[160]="eject ";unsigned n=strlen(target);
 if(n>sizeof(line)-8){close(fd);return -1;}
 memcpy(line+6,target,n);line[6+n]='\n';
 int ok=write(fd,line,7+n)==(int)(7+n);
 close(fd);return ok?0:-1;
}
int umount(const char *target){
 static int (*real)(const char*);
 if(!request_eject(target))return 0;
 if(!real)real=dlsym((void*)-1,"umount");
 return real?real(target):(errno=ENOSYS,-1);
}
int umount2(const char *target,int flags){
 static int (*real)(const char*,int);
 if(!request_eject(target))return 0;
 if(!real)real=dlsym((void*)-1,"umount2");
 return real?real(target,flags):(errno=ENOSYS,-1);
}
