#include <pybind11/pybind11.h>
#include <pybind11/numpy.h>
#include <iostream>
#include <thread>
#include <future>
#include <mutex>
#include <cmath>
#include <chrono>
#ifdef _WIN32
    #include <Winsock2.h>
    #include <ws2tcpip.h>
    #pragma comment(lib, "Ws2_32.lib")
#elif __linux__
    #include <sys/socket.h>
    #include <netinet/in.h>
    #include <fcntl.h>
#endif
#include "WzSerialportPlus.h"
#include "mmw_example_nonos.h"
#include "unlock_queue.h"

#ifdef __GNUC__
#define PACK( __Declaration__ ) __Declaration__ __attribute__((__packed__))
#endif

#ifdef _MSC_VER
#define PACK( __Declaration__ ) __pragma( pack(push, 1) ) __Declaration__ __pragma( pack(pop) )
#endif

#define STRINGIFY(x) #x
#define MACRO_STRINGIFY(x) STRINGIFY(x)

namespace py = pybind11;

WzSerialportPlus radar_serial_port;
int memidx=0;
int overflowCnt=0;
int packetSize_g=1466;
uint32_t packetNum_g=1000;
uint8_t *serial_buf_ptr=NULL;
std::mutex serial_buf_mutex;
std::mutex udp_mutex;
std::atomic<bool> udp_continue_g=true;
std::mutex udp_async_mutex;
std::future<pybind11::array_t<uint8_t>> udp_val;
uint32_t receivedPacketNum_g=0,firstPacketNum_g=0,lastPacketNum_g=0,expectedPacketNum_g=0;
#define packetSize_d 1466
PACK(typedef struct {
    uint32_t seqNum;
    uint8_t byteCnt[6];
    uint8_t payload[packetSize_d-10];
}) packet_t;
std::thread udp_thread_g;
// ── v2.0 frame ring buffer + reorder-tolerant assembly ──────────────────────
// The UDP receive thread writes each packet's payload directly (in-place) into
// its destination slot in this ring by ABSOLUTE offset (derived from seqNum), so
// the hot path is just one small memcpy per packet (like v1.3) — NO big per-frame
// memcpy/memset burst that would stall recvfrom and overflow the OS socket buffer.
//
// To tolerate out-of-order packet arrival, the producer keeps a small sliding
// window of REORDER_WIN in-progress frames, each with a received-packet BITMAP.
// Packets are never zeroed on the write path; a frame is only published once its
// bitmap is complete (no loss, zero added latency) or once it falls out of the
// window (loss); at that point the byte ranges of the genuinely-missing packets
// are zero-filled. Reordered packets land at their correct offset regardless of
// arrival order. Heavy copy-out of complete frames happens in the consumer.
#define REORDER_WIN 2     // tolerate reorder spanning up to (REORDER_WIN-1) frames
uint8_t  *frameRing_g   = NULL;   // frameSlots_g * bytesInFrame_g bytes
uint32_t *frameLost_g   = NULL;   // per-slot count of packets lost in that frame (for stats)
uint32_t  frameSlots_g  = 0;      // number of frame slots in the ring
int       bytesInFrame_g= 0;      // bytes per radar frame (set in udp_read_thread_init)
std::atomic<uint64_t> frameIn_g{0};   // total frames published by producer
std::atomic<uint64_t> frameOut_g{0};  // total frames consumed
std::mutex frameOut_mutex;            // serialises frameOut updates (producer override + consumer)
uint64_t  alignBase_g   = 0;      // absolute byte of the very first frame boundary (after align)
uint32_t  framePutCnt_g = 0;      // total whole frames published (monotonic)
bool      asmInited_g   = false;  // false until the first frame boundary is locked
// Sliding reorder window state (producer-only): one bitmap + received count per
// in-progress frame, indexed by (frameIdx % REORDER_WIN).
uint8_t  *bm_g          = NULL;   // REORDER_WIN bitmaps of received packets
uint32_t  bmBytes_g     = 0;      // bytes per bitmap
uint32_t  recvCnt_g[REORDER_WIN] = {0};  // received packets per in-progress frame slot

static inline uint8_t* slotPtr(uint64_t frameIdx){
    return frameRing_g + (size_t)(frameIdx % frameSlots_g) * (size_t)bytesInFrame_g;
}
static inline uint8_t* bmPtr(uint64_t frameIdx){
    return bm_g + (size_t)(frameIdx % REORDER_WIN) * (size_t)bmBytes_g;
}
// First packet seq number and packet count contributing to a frame (deterministic).
static inline uint32_t frameFirstSeq(uint64_t f){
    return (uint32_t)((alignBase_g + f * (uint64_t)bytesInFrame_g) / (packetSize_d - 10)) + 1;
}
static inline uint32_t frameExpCount(uint64_t f){
    uint64_t fb = alignBase_g + f * (uint64_t)bytesInFrame_g;
    uint32_t first = (uint32_t)(fb / (packetSize_d - 10)) + 1;
    uint32_t last  = (uint32_t)((fb + bytesInFrame_g - 1) / (packetSize_d - 10)) + 1;
    return last - first + 1;
}
static inline void openFrame(uint64_t f){      // reset bitmap/count as a frame enters the window
    memset(bmPtr(f), 0, bmBytes_g);
    recvCnt_g[f % REORDER_WIN] = 0;
}

int add(int i, int j) {
    return i+j;
}

bool radar_start_read_thread(std::string portName, int baudrate, uint32_t packetNum, int packetSize){
    if(serial_buf_ptr!=NULL)
        return false;
    serial_buf_ptr = new uint8_t[packetSize*packetNum];//initialize buffer
    packetNum_g=packetNum;
    packetSize_g=packetSize;

    radar_serial_port.setReceiveCalback([&](char* data,int length){//Set the receive callback function and execute it after receiving the data
        serial_buf_mutex.lock();//exclusive buffer
        if(memidx+length>packetSize_g*packetNum_g){//Guaranteed not to exceed the buffer range
            overflowCnt++;
            printf("[fpga_udp]serial buffer overflowed for %d time(s)!memidx=%d,length=%d,packetSize=%d,packetNum=%d\n",overflowCnt,memidx,length,packetSize_g,packetNum_g);
            memidx=0;
        }
        memcpy(serial_buf_ptr+memidx,data,length);//Copy the received data into the buffer
        memidx+=length;
        serial_buf_mutex.unlock();
    });

    #ifdef _WIN32
        if (portName.substr(0,3)=="COM" && stoi(portName.substr(3))>=10)
            portName="\\\\.\\"+portName;//port name has prefix in windows
    #endif
    return radar_serial_port.open(portName,baudrate,1,8,'n');//Open the port to start the thread to receive data
}

py::array_t<uint8_t> get_radar_buf(){
    serial_buf_mutex.lock();//exclusive buffer
    auto result = py::array_t<uint8_t>(memidx);
    py::buffer_info buf = result.request();
    uint8_t *buf_ptr = static_cast<uint8_t *>(buf.ptr);
    memcpy(buf_ptr,serial_buf_ptr,memidx);
    memidx=0;
    serial_buf_mutex.unlock();
    return result;
}

void radar_stop_read_thread(){
    radar_serial_port.close();
    serial_buf_mutex.lock();
    delete[] serial_buf_ptr;
    serial_buf_ptr=NULL;
    memidx=0;
    overflowCnt=0;
    serial_buf_mutex.unlock();
}

void _read_data_udp(int sock_fd, uint32_t frameNum, int bytesInFrame, int packetSize, int &lastFrameRemainBytes, int timeout_s, py::array_t<uint8_t> buf){
    udp_mutex.lock();

    if(packetSize%2 != 0){
        throw std::runtime_error("[fpga_udp]Number of packetSize must be even");
    }

    py::buffer_info buf_info = buf.request();
    uint8_t *buf_ptr = static_cast<uint8_t *>(buf_info.ptr);

    // Time out
    #ifdef __linux__
        int sockfd = sock_fd;
        struct timeval tv;
        tv.tv_sec  = timeout_s;
        tv.tv_usec = 0;
        setsockopt(sockfd, SOL_SOCKET, SO_RCVTIMEO, (const char*)&tv, sizeof(struct timeval));
    #elif _WIN32
        SOCKET sockfd = (SOCKET)sock_fd;
        int TimeOut = timeout_s*1000;
        setsockopt(sockfd, SOL_SOCKET, SO_RCVTIMEO, (const char*)&TimeOut, sizeof(TimeOut));
    #endif

    struct sockaddr_in src;
    socklen_t src_len = sizeof(src);
    memset(&src, 0, sizeof(src));

    uint32_t seqNum=0;
    uint64_t bytesCnt;
    int receivePacketLen=0,lastFrameTransferedBytes;

    // Wait for start of next frame
    do{
        receivePacketLen=recvfrom(sockfd, (char*)&buf_ptr[0], packetSize, 0, (sockaddr*)&src, &src_len);
        if (receivePacketLen < 0){      //blocking receive timeout
            printf("[fpga_udp]udp time out\n");
            udp_mutex.unlock();
            lastFrameRemainBytes=-1;
            return;
        }
        seqNum = *(uint32_t *)buf_ptr;//first 4 bytes: sequence number for every packet
        bytesCnt = (seqNum-1)*(packetSize-10);
        lastFrameTransferedBytes = bytesCnt % bytesInFrame;
        lastFrameRemainBytes = (bytesInFrame-lastFrameTransferedBytes) % bytesInFrame;
    }while(lastFrameRemainBytes>=packetSize-10);
    
    expectedPacketNum_g = ceil((lastFrameRemainBytes + frameNum*bytesInFrame)/((double)(packetSize-10)));

    // Read in the rest of the frame
    for(int i=1;i<expectedPacketNum_g;i++){
        int idx = i*packetSize;
        // if(idx>=packetSize*packetNum){
        //     throw std::runtime_error("Array Index Out Of Bounds!");
        // }
        receivePacketLen=recvfrom(sockfd, (char*)&buf_ptr[idx], packetSize, 0, (sockaddr*)&src, &src_len);
        if (receivePacketLen < 0){      //blocking receive timeout
            printf("[fpga_udp]udp time out\n");
            break;
        }
    }

    udp_mutex.unlock();
}

py::array_t<uint8_t> postProc_packet_sort(py::array_t<uint8_t> buf, uint32_t frameNum, int bytesInFrame, int packetSize, int lastFrameRemainBytes, uint32_t &receivedPacketNum, uint32_t &firstPacketNum, uint32_t &lastPacketNum){
    int payloadSize = packetSize-10, cpySize;
    uint32_t packetNum = ceil((lastFrameRemainBytes + frameNum*bytesInFrame)/((double)(packetSize-10)));
    uint32_t seqNum=0,idx=0;

    auto result = py::array_t<uint8_t>(frameNum*bytesInFrame);
    py::buffer_info sort_buf_info = result.request();
    uint8_t *sort_buf_ptr = static_cast<uint8_t *>(sort_buf_info.ptr);

    py::buffer_info buf_info = buf.request();
    uint8_t *buf_ptr = static_cast<uint8_t *>(buf_info.ptr);

    //first packet
    firstPacketNum = *(uint32_t *)buf_ptr;//4 bytes of sequence number for every packet
    cpySize = payloadSize-lastFrameRemainBytes;
    memcpy(sort_buf_ptr, &buf_ptr[lastFrameRemainBytes], cpySize);
    receivedPacketNum = 1;

    //rest packet
    for(uint32_t i=1;i<packetNum;i++){
        seqNum=*(uint32_t *)&buf_ptr[i*packetSize];//4 bytes of sequence number for every packet
        //byteCnt=*(uint32_t *)&buf_ptr[i*packetSize+4];//6 bytes of byte count – provides information about number of bytes transmitted till last packet.
        idx = seqNum-firstPacketNum;
        if(idx<1 || idx>packetNum-1) continue;//out of boundary
        if(idx==packetNum-1){//last packet
            memcpy(&sort_buf_ptr[(idx-1)*payloadSize+cpySize], &buf_ptr[i*packetSize+10], frameNum*bytesInFrame-idx*payloadSize+lastFrameRemainBytes);
        }else{//middle packet
            memcpy(&sort_buf_ptr[(idx-1)*payloadSize+cpySize], &buf_ptr[i*packetSize+10], payloadSize);//1456 bytes of payload
        }
        receivedPacketNum++;
    }

    lastPacketNum = *(uint32_t *)&buf_ptr[(packetNum-1)*packetSize];
    // printf("[fpga_udp]received packet num:%d,expected packet num:%d\n",receivedPacketNum,packetNum);

    return result;
}

// void _read_data_udp_buffered(int sock_fd, uint32_t bufFrameNum, int bytesInFrame, int packetSize, int timeout_s){
//     uint32_t maxPacketNum = ((bufFrameNum+1)*bytesInFrame)/(packetSize-10)+1;
//     firstBufFull=false;
//     auto packetBuf = py::array_t<uint8_t>(maxPacketNum*packetSize);
//     doubleBuffer[0] = py::array_t<uint8_t>(bufFrameNum*bytesInFrame);
//     doubleBuffer[1] = py::array_t<uint8_t>(bufFrameNum*bytesInFrame);
// }

py::array_t<uint8_t> read_data_udp(int sock_fd, uint32_t frameNum, int bytesInFrame, int packetSize, int timeout_s, bool sort){
    uint32_t maxPacketNum = ((frameNum+1)*bytesInFrame)/(packetSize-10)+1;
    /* No pointer is passed, so NumPy will allocate the buffer */
    auto result = py::array_t<uint8_t>(maxPacketNum*packetSize);
    int lastFrameRemainBytes=0;

    _read_data_udp(sock_fd,frameNum,bytesInFrame,packetSize,lastFrameRemainBytes,timeout_s,result);

    if(sort) return postProc_packet_sort(result,frameNum,bytesInFrame,packetSize,lastFrameRemainBytes,receivedPacketNum_g,firstPacketNum_g,lastPacketNum_g);

    return result;
}

py::array_t<uint8_t> read_data_udp_block_thread(int sock_fd, uint32_t frameNum, int bytesInFrame, int packetSize, int timeout_s, bool sort){
    uint32_t maxPacketNum = ((frameNum+1)*bytesInFrame)/(packetSize-10)+1;
    /* No pointer is passed, so NumPy will allocate the buffer */
    auto result = py::array_t<uint8_t>(maxPacketNum*packetSize);
    int lastFrameRemainBytes=0;

    std::thread udp_thread(_read_data_udp,sock_fd,frameNum,bytesInFrame,packetSize,std::ref(lastFrameRemainBytes),timeout_s,result);
    udp_thread.join();

    if(sort) return postProc_packet_sort(result,frameNum,bytesInFrame,packetSize,lastFrameRemainBytes,receivedPacketNum_g,firstPacketNum_g,lastPacketNum_g);

    return result;
}

void read_data_udp_async_start(int sock_fd, uint32_t frameNum, int bytesInFrame, int packetSize, int timeout_s, bool sort){
    udp_async_mutex.lock();
    udp_val = std::async(std::launch::async, read_data_udp, sock_fd,frameNum,bytesInFrame,packetSize,timeout_s,sort);
}

py::array_t<uint8_t> read_data_udp_async_wait(){
    udp_async_mutex.unlock();
    return udp_val.get();
}

// Publish the oldest in-progress frame (= frameIn_g) to the consumer and slide
// the window up. Zero-fills the byte ranges of packets that never arrived (from
// the bitmap) — in the no-loss case that loop does nothing, so there is NO big
// memset/burst. Only touches indices under a ns-scale lock. Producer only.
static inline void flushOldest(){
    const int payloadSize = packetSize_d - 10;
    uint64_t f   = frameIn_g.load(std::memory_order_relaxed);
    uint64_t fb  = alignBase_g + f * (uint64_t)bytesInFrame_g;
    uint32_t exp = frameExpCount(f);
    uint32_t firstSeq = frameFirstSeq(f);
    uint8_t *slot = slotPtr(f);
    uint8_t *bm   = bmPtr(f);

    // Zero only the holes left by packets that never arrived for this frame.
    for(uint32_t i = 0; i < exp; i++){
        if(!(bm[i >> 3] & (1u << (i & 7)))){
            uint64_t a  = (uint64_t)(firstSeq + i - 1) * payloadSize;
            uint64_t lo = std::max(a, fb);
            uint64_t hi = std::min(a + payloadSize, fb + (uint64_t)bytesInFrame_g);
            if(lo < hi) memset(slot + (size_t)(lo - fb), 0, (size_t)(hi - lo));
        }
    }
    uint32_t rc = recvCnt_g[f % REORDER_WIN];
    frameLost_g[f % frameSlots_g] = (rc >= exp) ? 0 : (exp - rc);

    {   // override: drop oldest unconsumed frame if the ring would overflow
        std::lock_guard<std::mutex> lk(frameOut_mutex);
        uint64_t out = frameOut_g.load(std::memory_order_relaxed);
        if(f + 1 - out > frameSlots_g - REORDER_WIN)
            frameOut_g.store(f + 1 - (frameSlots_g - REORDER_WIN), std::memory_order_relaxed);
    }
    frameIn_g.store(f + 1, std::memory_order_release);   // publish
    framePutCnt_g++;
    openFrame(f + REORDER_WIN);   // a fresh frame enters the top of the window
}

void _udp_read_thread(int sock_fd){
    udp_mutex.lock();

    #ifdef __linux__
        int sockfd = sock_fd;
        int status = fcntl(sockfd, F_SETFL, fcntl(sockfd, F_GETFL, 0) | O_NONBLOCK); // non-blocking
        if(status == -1)
            throw std::runtime_error("[fpga_udp]error calling fcntl to set O_NONBLOCK");
    #elif _WIN32
        SOCKET sockfd = (SOCKET)sock_fd;
        unsigned long mode = 1; // 1 to enable non-blocking socket
        int status = ioctlsocket(sockfd, FIONBIO, &mode);
        if(status != 0)
            throw std::runtime_error("[fpga_udp]ioctlsocket failed:"+std::to_string(WSAGetLastError()));
    #endif

    struct sockaddr_in src;
    socklen_t src_len = sizeof(src);
    memset(&src, 0, sizeof(src));

    const int payloadSize = packetSize_d - 10;   // 1456 bytes of ADC payload per packet
    const uint64_t frame  = (uint64_t)bytesInFrame_g;
    int n;
    packet_t buffer;

    while (udp_continue_g) {
        // Non-blocking receive of one UDP packet from the DCA1000.
        n = recvfrom(sockfd, (char *)&buffer, packetSize_d,
                     0, (sockaddr*)&src, &src_len);
        if (n <= 0)          // no data available right now
            continue;

        uint32_t seqNum  = buffer.seqNum;                         // 1-based packet index
        uint64_t absByte = (uint64_t)(seqNum - 1) * payloadSize;  // payload start, absolute
        uint64_t endByte = absByte + payloadSize;

        // Lock onto the first whole-frame boundary at/after the first packet seen,
        // discarding at most one partial frame — once, ever. Open the initial window.
        if(!asmInited_g){
            alignBase_g = ((absByte + frame - 1) / frame) * frame;
            for(int w = 0; w < REORDER_WIN; w++) openFrame((uint64_t)w);
            asmInited_g = true;
        }

        // Ignore the part of this packet that belongs to already-published frames.
        uint64_t in = frameIn_g.load(std::memory_order_relaxed);
        uint64_t pubBoundary = alignBase_g + in * frame;
        uint64_t s = std::max(absByte, pubBoundary);
        if(s >= endByte)          // entirely within already-published frames → drop
            continue;

        uint64_t fStart = (s - alignBase_g) / frame;
        uint64_t fEnd   = (endByte - 1 - alignBase_g) / frame;   // 1 frame, or 2 if straddling

        // Make room: if this packet reaches beyond the window, publish the oldest
        // frame(s) (their missing packets are presumed lost and zero-filled).
        while(fEnd > frameIn_g.load(std::memory_order_relaxed) + (REORDER_WIN - 1))
            flushOldest();

        // Write each touched frame's portion in place, and mark the bitmap(s).
        for(uint64_t f = fStart; f <= fEnd; f++){
            uint64_t fb = alignBase_g + f * frame;
            uint64_t lo = std::max(s, fb);
            uint64_t hi = std::min(endByte, fb + frame);
            if(lo < hi)
                memcpy(slotPtr(f) + (size_t)(lo - fb),
                       buffer.payload + (size_t)(lo - absByte), (size_t)(hi - lo));
            uint32_t bit = seqNum - frameFirstSeq(f);
            if(bit < frameExpCount(f)){
                uint8_t *bm = bmPtr(f);
                if(!(bm[bit >> 3] & (1u << (bit & 7)))){
                    bm[bit >> 3] |= (uint8_t)(1u << (bit & 7));
                    recvCnt_g[f % REORDER_WIN]++;
                }
            }
        }

        // Publish, in order, every oldest frame that is now complete (no loss) —
        // this is the zero-latency common path.
        for(;;){
            uint64_t f = frameIn_g.load(std::memory_order_relaxed);
            if(recvCnt_g[f % REORDER_WIN] < frameExpCount(f)) break;
            flushOldest();
        }
    }

    udp_mutex.unlock();
}

py::array_t<uint8_t> udp_read_thread_get_frames(uint32_t frameNum, int bytesInFrame, int timeout_s, bool sort){
    (void)sort; (void)bytesInFrame;   // frames already assembled & ordered; geometry from bytesInFrame_g

    // Wait (poll at 1 ms) until frameNum whole frames are available, or timeout.
    uint64_t out = frameOut_g.load(std::memory_order_acquire);
    uint32_t waited = 0, timeout_ms = (uint32_t)timeout_s * 1000;
    while(frameIn_g.load(std::memory_order_acquire) - out < frameNum){
        if(waited++ >= timeout_ms) break;
        std::this_thread::sleep_for(std::chrono::milliseconds(1));
    }

    // Reserve the frames under the lock (so producer override won't drop them while
    // we copy), then copy them out — the heavy copy stays off the receive thread.
    uint32_t take;
    {
        std::lock_guard<std::mutex> lk(frameOut_mutex);
        out = frameOut_g.load(std::memory_order_relaxed);
        uint64_t avail = frameIn_g.load(std::memory_order_acquire) - out;
        take = (uint32_t)std::min<uint64_t>(frameNum, avail);
        frameOut_g.store(out + take, std::memory_order_relaxed);   // reserve/consume now
    }
    if(take < frameNum)
        printf("[fpga_udp]udp time out (got %u/%u frames)\n", take, frameNum);

    // Per-batch packet stats for the frames actually returned (so the verbose
    // getters describe THIS batch — first/last packet num vary, loss is real).
    if(take){
        const int payloadSize = packetSize_d - 10;
        uint64_t firstByte = alignBase_g + out * (uint64_t)bytesInFrame_g;
        uint64_t lastByte  = alignBase_g + (out + take) * (uint64_t)bytesInFrame_g - 1;
        uint32_t firstSeq  = (uint32_t)(firstByte / payloadSize) + 1;
        uint32_t lastSeq   = (uint32_t)(lastByte  / payloadSize) + 1;
        uint32_t expected  = lastSeq - firstSeq + 1;
        uint32_t lost = 0;
        for(uint32_t k = 0; k < take; k++) lost += frameLost_g[(out + k) % frameSlots_g];
        firstPacketNum_g    = firstSeq;
        lastPacketNum_g     = lastSeq;
        expectedPacketNum_g = expected;
        receivedPacketNum_g = (lost >= expected) ? 0 : (expected - lost);
    } else {
        firstPacketNum_g = lastPacketNum_g = expectedPacketNum_g = receivedPacketNum_g = 0;
    }

    size_t outBytes = (size_t)take * (size_t)bytesInFrame_g;
    auto result = py::array_t<uint8_t>(outBytes);
    if(take){
        uint8_t *dst = static_cast<uint8_t *>(result.request().ptr);
        for(uint32_t k = 0; k < take; k++)
            memcpy(dst + (size_t)k * bytesInFrame_g, slotPtr(out + k), (size_t)bytesInFrame_g);
    }
    return result;
}

PYBIND11_MODULE(fpga_udp, m) {
    m.doc() = R"pbdoc(
        FPGA UDP reader & mmwavelink API plugin
        -----------------------

        .. currentmodule:: fpga_udp

        .. autosummary::
           :toctree: _generate

           add
           subtract

           radar_start_read_thread
           get_radar_buf
           radar_stop_read_thread
           getOverflowCnt

           read_data_udp
           read_data_udp_block_thread
           read_data_udp_async_start
           read_data_udp_async_wait
           get_receivedPacketNum
           get_firstPacketNum
           get_lastPacketNum
           get_expectedPacketNum
           udp_read_thread_init
           udp_read_thread_start
           udp_read_thread_get_frames
           udp_read_thread_stop

           AWR2243_firmwareDownload
           AWR2243_init
           AWR2243_setFrameCfg
           AWR2243_sensorStart
           AWR2243_isSensorStarted
           AWR2243_waitSensorStop
           AWR2243_sensorStop
           AWR2243_sensorStartCont
           AWR2243_sensorStopCont
           AWR2243_poweroff
           AWR2243_test_demo_app
    )pbdoc";

    m.def("radar_start_read_thread", &radar_start_read_thread);
    m.def("get_radar_buf", &get_radar_buf);
    m.def("radar_stop_read_thread", &radar_stop_read_thread);
    m.def("getOverflowCnt", []() {
        return overflowCnt;
    });
    m.def("reset_radar_buf", []() {
        serial_buf_mutex.lock();
        memidx=0;
        overflowCnt=0;
        serial_buf_mutex.unlock();
    });

    m.def("read_data_udp", &read_data_udp, R"pbdoc(
        read UDP packet from FPGA as fast as written in C.

        sockfd:\tan open file descriptor of a socket
        frameNum:\tA total of frameNum frames will be read
        bytesInFrame:\tbytes of one frame
        packetSize:\tand the size of each packet is packetSize
        timeout_s:\ttime out for udp in sec
    )pbdoc");
    m.def("read_data_udp_block_thread", &read_data_udp_block_thread, R"pbdoc(
        read UDP packet from FPGA using thread, block untill data transfer finished.

        sockfd:\tan open file descriptor of a socket
        frameNum:\tA total of frameNum frames will be read
        bytesInFrame:\tbytes of one frame
        packetSize:\tand the size of each packet is packetSize
        timeout_s:\ttime out for udp in sec
    )pbdoc");
    m.def("read_data_udp_async_start", &read_data_udp_async_start, R"pbdoc(
        read UDP packet from FPGA using async, return immediately.

        sockfd:\tan open file descriptor of a socket
        frameNum:\tA total of frameNum frames will be read
        bytesInFrame:\tbytes of one frame
        packetSize:\tand the size of each packet is packetSize
        timeout_s:\ttime out for udp in sec
    )pbdoc");
    m.def("read_data_udp_async_wait", &read_data_udp_async_wait, R"pbdoc(
        read UDP packet from FPGA using async, wait previous async task finished.
    )pbdoc");
    m.def("get_receivedPacketNum", []() {return receivedPacketNum_g;});
    m.def("get_firstPacketNum", []() {return firstPacketNum_g;});
    m.def("get_lastPacketNum", []() {return lastPacketNum_g;});
    m.def("get_expectedPacketNum", []() {return expectedPacketNum_g;});
    m.def("udp_read_thread_init", [](int bytesInFrame, int frameNumInBuf) {
        // v2.0: in-place frame ring + reorder-tolerant bitmap window. The receive
        // thread assembles packets directly into slots; the consumer copies whole
        // frames out. Returns capacity in BYTES.
        bytesInFrame_g = bytesInFrame;
        frameSlots_g = (uint32_t)std::max(REORDER_WIN + 1, frameNumInBuf);
        frameRing_g = new uint8_t[(size_t)frameSlots_g * (size_t)bytesInFrame];
        memset(frameRing_g, 0, (size_t)frameSlots_g * (size_t)bytesInFrame);
        frameLost_g = new uint32_t[frameSlots_g]();
        // bitmap big enough for all packets that can touch one frame (+margin)
        uint32_t maxPkts = (uint32_t)(bytesInFrame / (packetSize_d - 10)) + 2;
        bmBytes_g = (maxPkts + 7) / 8;
        bm_g = new uint8_t[(size_t)REORDER_WIN * bmBytes_g]();
        for(int w = 0; w < REORDER_WIN; w++) recvCnt_g[w] = 0;
        frameIn_g.store(0); frameOut_g.store(0);
        alignBase_g = 0;
        asmInited_g = false;
        framePutCnt_g = 0;
        receivedPacketNum_g = expectedPacketNum_g = firstPacketNum_g = lastPacketNum_g = 0;
        return (size_t)frameSlots_g * (size_t)bytesInFrame;
    });
    m.def("udp_read_thread_start", [](int sock_fd) {
        udp_continue_g = true;
        udp_thread_g = std::thread(_udp_read_thread, sock_fd);
    });
    m.def("udp_read_thread_get_frames", &udp_read_thread_get_frames);
    m.def("udp_read_thread_isStarted", [](){return udp_continue_g.load();});
    m.def("get_framePutCnt", [](){return framePutCnt_g;});
    m.def("udp_read_thread_stop", []() {
        if(udp_continue_g){
            udp_continue_g = false;
            udp_thread_g.join();
            delete[] frameRing_g;  frameRing_g = NULL;
            delete[] frameLost_g;  frameLost_g = NULL;
            delete[] bm_g;         bm_g = NULL;
            frameSlots_g = 0;
            asmInited_g = false;
        }
    });
    m.def("kfifo_benchmark", [](uint32_t loopTime,uint32_t maxPacketNum) {
        UnlockQueue<packet_t> *bench_q = new UnlockQueue<packet_t>(maxPacketNum);
        packet_t buffer;
        auto start = std::chrono::system_clock::now();
        uint32_t realCnt=0;
        for(uint32_t i=0;i<loopTime;i++){
            buffer.seqNum=i;
            uint32_t ret=bench_q->Put(&buffer, 1);
            if(ret!=1){
                printf("%d\n",ret);
                break;
            }
            realCnt++;
        }
        auto end   = std::chrono::system_clock::now();
        auto duration = std::chrono::duration_cast<std::chrono::microseconds>(end - start);
        auto dur_s=double(duration.count()) * std::chrono::microseconds::period::num / std::chrono::microseconds::period::den;
        std::cout<<realCnt<<" loops cost "<<dur_s<<"sec, "<< realCnt*sizeof(packet_t)/dur_s/1024/1024 <<"MB/s"<<std::endl;

        delete bench_q;
    });

    m.def("AWR2243_firmwareDownload",[](){
        return MMWL_App_firmwareDownload(RL_DEVICE_MAP_CASCADED_1);
    }, R"pbdoc(
        This function downloads meta image patch to external serial flash.
        Once the flashing operation is successful, 
        it is not necessary to download the meta image in the subsequent power cycles.
        If no flash connected or not downloaded successfully, you must set downloadFw=true
        when you call AWR2243_init and set EnableFwDownload=1 in mmwaveconfig.txt and
        MMWL_FILETYPE_META_IMG = 0 in mmw_example_nonos.h.
    )pbdoc");
    m.def("AWR2243_init",[](std::string configFilename){
        return MMWL_App_init(RL_DEVICE_MAP_CASCADED_1, configFilename.c_str(), false);
    });
    m.def("AWR2243_setFrameCfg",[](int numFrames){
        return MMWL_App_setFrameCfg(RL_DEVICE_MAP_CASCADED_1, numFrames);
    });
    m.def("AWR2243_sensorStart",[](){
        return MMWL_App_startSensor(RL_DEVICE_MAP_CASCADED_1);
    });
    m.def("AWR2243_isSensorStarted",[](){
        return MMWL_App_isSensorStarted();
    });
    m.def("AWR2243_waitSensorStop",[](){
        return MMWL_App_waitSensorStop(RL_DEVICE_MAP_CASCADED_1);
    });
    m.def("AWR2243_sensorStop",[](){
        return MMWL_App_stopSensor(RL_DEVICE_MAP_CASCADED_1);
    });
    m.def("AWR2243_sensorStartCont",[](){
        return MMWL_App_startCont(RL_DEVICE_MAP_CASCADED_1);
    }, R"pbdoc(
        This function starts the FMCW radar in continous mode.
        In continuous mode, the signal is not frequency modulated but has the same frequency over time.
        Note: The continuous streaming mode configuration APIs are supported only for debug purpose.
        Please refer latest DFP release note for more info.
    )pbdoc");
    m.def("AWR2243_sensorStopCont",[](){
        return MMWL_App_stopCont(RL_DEVICE_MAP_CASCADED_1);
    });
    m.def("AWR2243_poweroff",[](){
        return MMWL_App_poweroff(RL_DEVICE_MAP_CASCADED_1);
    });
    m.def("AWR2243_test_demo_app",[](std::string configFilename){
        return MMWL_App(configFilename.c_str());
    });
    
    m.def("add", &add, R"pbdoc(
        Add two numbers
    )pbdoc");

    m.def("subtract", [](int i, int j) {
        return i - j;
    });

#ifdef VERSION_INFO
    m.attr("__version__") = MACRO_STRINGIFY(VERSION_INFO);
#else
    m.attr("__version__") = "dev";
#endif
}
