/* C trampolines for the Nikon MAID3 bindings (filmscan-studio).
 *
 * The MAID3 module calls client callbacks with raw C function pointers from
 * inside its own worker paths. Calling back into Python (libffi) at those
 * moments means re-entering the interpreter on the module's stack, which
 * deadlocks against our own calls into the module. So Python registers these
 * tiny C stubs instead: they record arguments into static buffers and return.
 * Python polls the buffers between its own calls, where copying is safe.
 *
 * The data trampoline accumulates delivered chunks into a C-side buffer
 * (the module frees its blob the moment the callback returns, so the copy
 * MUST happen here, not in Python), which Python reads once the acquire
 * command completes.
 */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <CoreFoundation/CoreFoundation.h>

/* The Mac PTP driver delivers device-attach events through ImageCaptureCore,
 * which dispatchs on the main run loop. The sample program spins
 * CFRunLoopRunInMode for a second before looking for devices; we tick the
 * same loop from Python between Async pumps. */
void fsk_runloop_tick(double seconds) {
    CFRunLoopRunInMode(kCFRunLoopDefaultMode, (CFTimeInterval)seconds, true);
}

/* Layout-compatible stand-ins for the MAID structs (natural alignment,
 * macOS LP64: ULONG=u32, BOOL=char per NkTypes.h). */
typedef struct {
    uint32_t ulType;
    uint32_t ulID;
    void    *refClient;
    void    *refModule;
} ObjRaw;

typedef struct {
    uint32_t ulID;
    uint32_t ulType;
    uint32_t ulVisibility;
    uint32_t ulOperations;
    char     szDescription[256];
} CapInfoRaw;

typedef struct {                       /* NkMAIDDataInfo base inlined */
    uint32_t ulType;
    uint32_t ulFileDataType;
    uint32_t ulTotalLength;
    uint32_t ulStart;
    uint32_t ulLength;
    char     fDiskFile;
    char     fRemoveObject;
} FileInfoRaw;

typedef struct {
    uint32_t ulType;                   /* base */
    uint32_t szTotalPixels_w;
    uint32_t szTotalPixels_h;
    uint32_t ulColorSpace;
    int32_t  rData_x;
    int32_t  rData_y;
    uint32_t rData_w;
    uint32_t rData_h;
    uint32_t ulRowBytes;
    uint16_t wBits[4];
    uint16_t wPlane;
    char     fRemoveObject;
} ImageInfoRaw;

typedef int32_t (*EntryFn)(void *obj, uint32_t cmd, uint32_t param,
                           uint32_t dt, uint64_t data, void *done, void *ref);

static EntryFn g_entry;

void fsk_set_entry(uint64_t fn) { g_entry = (EntryFn)fn; }

int32_t fsk_call(uint64_t obj, uint32_t cmd, uint32_t param, uint32_t dt,
                 uint64_t data, uint64_t done, uint64_t ref) {
    if (!g_entry) return -999;
    return g_entry((void *)obj, cmd, param, dt, data, (void *)done, (void *)ref);
}

/* ---- event trampoline ---------------------------------------------------- */
volatile int g_event_fired = 0;
uint64_t g_event_buf[3];   /* refClient, ulEvent, data */

void fsk_event_trampoline(void *refClient, uint32_t ulEvent, uint64_t data) {
    g_event_buf[0] = (uint64_t)refClient;
    g_event_buf[1] = (uint64_t)ulEvent;
    g_event_buf[2] = data;
    g_event_fired = 1;
}
void *fsk_event_trampoline_addr(void) { return (void *)fsk_event_trampoline; }
void *fsk_event_data_ptr(void) { return g_event_buf; }
int   fsk_event_fired(void) { return g_event_fired; }
void  fsk_event_reset(void) { g_event_fired = 0; }

/* UI requests: auto-ack so the module is never waiting on a dialog we
 * cannot show (it may ask e.g. "turn camera control on"). Returns Ok. */
uint32_t fsk_ui_trampoline(void *refProc, void *pUIRequest) {
    (void)refProc; (void)pUIRequest;
    return 1;   /* kNkMAIDUIRequestResult_Ok */
}
void *fsk_ui_trampoline_addr(void) { return (void *)fsk_ui_trampoline; }

/* ---- completion trampoline ----------------------------------------------- */
volatile int g_completion_fired = 0;
uint64_t g_completion_buf[3];   /* cmd|param, dt|result, data */

void fsk_completion_trampoline(void *obj, uint32_t cmd, uint32_t param,
                               uint32_t dt, uint64_t data, void *ref,
                               int32_t result) {
    (void)obj; (void)ref;
    g_completion_buf[0] = ((uint64_t)cmd << 32) | param;
    g_completion_buf[1] = ((uint64_t)dt << 32) | (uint32_t)result;
    g_completion_buf[2] = data;
    g_completion_fired = 1;
}
void *fsk_completion_trampoline_addr(void) { return (void *)fsk_completion_trampoline; }
void *fsk_completion_data_ptr(void) { return g_completion_buf; }
int   fsk_completion_fired(void) { return g_completion_fired; }
void  fsk_completion_reset(void) { g_completion_fired = 0; }

/* ---- data trampoline + accumulation buffer -------------------------------- */
#define FSK_BLOB_CAP (128u * 1024u * 1024u)
static char    *g_blob;
static uint64_t g_blob_have;         /* max bytes written */
static uint64_t g_blob_total;
volatile int    g_data_fired = 0;
uint64_t        g_data_buf[3];       /* refClient, pInfo, pData */
uint32_t        g_data_kind;         /* last NkMAIDDataObjType seen */

int32_t fsk_data_trampoline(void *refClient, void *pInfo, void *pData) {
    g_data_buf[0] = (uint64_t)refClient;
    g_data_buf[1] = (uint64_t)pInfo;
    g_data_buf[2] = (uint64_t)pData;
    g_data_fired = 1;
    if (!g_blob) {
        g_blob = (char *)malloc(FSK_BLOB_CAP);
        if (!g_blob) return -116;    /* kNkMAIDResult_OutOfMemory */
    }
    uint32_t type = *(uint32_t *)pInfo;
    g_data_kind = type;
    if (type & 0x10) {               /* kNkMAIDDataObjType_File */
        FileInfoRaw *f = (FileInfoRaw *)pInfo;
        if ((uint64_t)f->ulStart + f->ulLength > FSK_BLOB_CAP) return -115;
        memcpy(g_blob + f->ulStart, pData, f->ulLength);
        uint64_t end = (uint64_t)f->ulStart + f->ulLength;
        if (end > g_blob_have) g_blob_have = end;
        if (f->ulTotalLength) g_blob_total = f->ulTotalLength;
    } else {                         /* image-plane delivery */
        ImageInfoRaw *im = (ImageInfoRaw *)pInfo;
        uint64_t total = (uint64_t)im->ulRowBytes * im->szTotalPixels_h;
        uint64_t off = (uint64_t)im->ulRowBytes * (uint64_t)im->rData_y;
        uint64_t n = (uint64_t)im->ulRowBytes * im->rData_h;
        if (off + n > FSK_BLOB_CAP) return -115;
        memcpy(g_blob + off, pData, n);
        if (off + n > g_blob_have) g_blob_have = off + n;
        if (total) g_blob_total = total;
    }
    return 0;                        /* kNkMAIDResult_NoError */
}
void *fsk_data_trampoline_addr(void) { return (void *)fsk_data_trampoline; }
int   fsk_data_fired(void) { return g_data_fired; }
void  fsk_data_reset(void) {
    g_data_fired = 0; g_blob_have = 0; g_blob_total = 0; g_data_kind = 0;
}
uint64_t fsk_data_have(void)  { return g_blob_have; }
uint64_t fsk_data_total(void) { return g_blob_total; }
uint32_t fsk_data_kind(void)  { return g_data_kind; }
int fsk_data_copy(char *dst, int max) {
    if (!g_blob || max <= 0) return 0;
    uint64_t n = g_blob_have;
    if ((uint64_t)max < n) n = max;
    memcpy(dst, g_blob, (size_t)n);
    return (int)n;
}
