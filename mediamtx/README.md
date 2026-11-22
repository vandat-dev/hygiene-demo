# MediaMTX – Camera RTSP giả lập

Đẩy video vào mediamtx để tạo luồng RTSP giống camera IP, dùng để phát triển và test khi không có camera thật.

```
tab Phát video ─WHIP┐                         ┌─RTSP────► reader.py   (BE)
edge / ffmpeg ─SRT/RTSP─► mediamtx path <tên> ─┤
                                               └─WebRTC──► tab Xem luồng (FE)
```

Mediamtx không cố định luồng nào. Ai đẩy vào path nào thì path đó tồn tại, và BE/FE đọc theo đúng tên path đó.

## Mục lục

- [Chạy nhanh](#chạy-nhanh)
- [Cấu trúc thư mục](#cấu-trúc-thư-mục)
- [Đưa video vào](#đưa-video-vào)
- [Port](#port)
  - [SRT](#srt)
- [Cách FE nhận WebRTC](#cách-fe-nhận-webrtc)
- [Xử lý sự cố](#xử-lý-sự-cố)
- [Phụ lục: toàn bộ port của MediaMTX](#phụ-lục-toàn-bộ-port-của-mediamtx)

## Chạy nhanh

Yêu cầu: Linux, Docker. Python 3 chỉ cần cho `reader.py`.

```bash
cd mediamtx

# 1. Khởi động mediamtx + web giao diện (service `web` chạy serve.py)
docker compose up -d
docker compose logs web     # in ra địa chỉ để mở, ví dụ http://192.168.1.11:8080

# 2. BE – lắng nghe luồng, tự chờ đến khi có luồng (URL đặt ở biến URL trong reader.py)
../.venv/bin/pip install -r requirements.txt
../.venv/bin/python reader.py
```

4. Mở http://localhost:8080. Ở tab **Phát video**, chọn video, giữ tên luồng `camera-upload`, rồi bấm **Bắt đầu phát**. `reader.py` sẽ tự kết nối và bắt đầu log.
5. Bấm **Xem luồng này →**, hoặc chuyển sang tab **Xem luồng**. Luồng vẫn tiếp tục phát khi chuyển tab; chấm đỏ trên tab Phát video cho biết đang phát.

### Xem từ máy khác trong mạng LAN

`serve.py` phục vụ giao diện giống `python3 -m http.server`, và có thêm đường dẫn `/lan-ip` trả về IP LAN hiện tại của máy. Khi tải, trang hỏi `/lan-ip` rồi dùng IP đó cho mọi URL (RTSP, link xem). Nếu mở bằng `localhost` hoặc `0.0.0.0`, trang tự chuyển sang IP LAN. Vì vậy chỉ cần gửi người khác địa chỉ trên thanh địa chỉ. IP đổi (do DHCP) thì cũng không phải sửa gì.

Nếu vẫn chạy bằng `python3 -m http.server`, trang vẫn hoạt động nhưng không tự biết IP: nó dùng đúng địa chỉ trên thanh địa chỉ, nên hãy mở bằng IP LAN.

- Nếu firewall đang bật, mở các port: `sudo ufw allow 8080/tcp && sudo ufw allow 8889/tcp && sudo ufw allow 8189/udp && sudo ufw allow 8554/tcp`.
- Nút **Sao chép** chỉ tự chép được trên `localhost` hoặc HTTPS. Trên IP LAN, nút này sẽ bôi đen URL để bạn nhấn Ctrl+C.

| Đích | URL |
|---|---|
| Phát video | http://192.168.1.11:8080/#/publish |
| Luồng RTSP | `rtsp://192.168.1.11:8554/<tên-path>` |
| Xem luồng | http://192.168.1.11:8080/#/view/<tên-path> |
| Trang xem có sẵn của mediamtx | http://192.168.1.11:8889/<tên-path> |

Lệnh thường dùng:

```bash
docker compose logs -f     # xem log
docker compose restart     # nạp lại sau khi sửa mediamtx.yml
docker compose down        # dừng
```

## Cấu trúc thư mục

| File | Mô tả |
|---|---|
| `docker-compose.yml` | Hai service, đều dùng `network_mode: host`: `mediamtx` (image `bluenviron/mediamtx:latest`) và `web` (image `python:3.12-alpine`, chạy `serve.py` ở port 8080) |
| `mediamtx.yml` | Cấu hình. Chỉ chứa các giá trị khác mặc định |
| `reader.py` | BE: lắng nghe RTSP bằng PyAV, log codec, độ phân giải, fps và frame. Tự chờ khi chưa có luồng và kết nối lại khi luồng dừng |
| `index.html` | Giao diện một trang, hai tab: **Phát video** (chọn file, đẩy vào mediamtx qua WHIP) và **Xem luồng** (nhận WebRTC qua WHEP, tự chờ và kết nối lại, nhớ các luồng đã xem gần đây) |
| `serve.py` | Web server cho giao diện, chạy trong service `web`. Có thêm `/lan-ip` để trang tự biết IP LAN của máy |
| `style.css` | Giao diện dùng chung cho hai trang (tự theo chế độ sáng/tối của hệ điều hành) |

## Đưa video vào

Path `all_others` trong `mediamtx.yml` cho phép đẩy vào path tên bất kỳ. Path chỉ tồn tại khi đang có luồng đẩy vào. Khi chưa có, đọc sẽ nhận `404 Not Found`, và `reader.py` sẽ tự thử lại mỗi 2 giây.

### Bằng trình duyệt (tab Phát video)

1. Mở http://localhost:8080 (tab **Phát video**)
2. Chọn hoặc kéo thả video, nhập tên luồng, rồi bấm **Bắt đầu phát**

Trình duyệt phát video trong thẻ `<video>`, lấy hình bằng `captureStream()`, nén thành H264 rồi đẩy lên mediamtx bằng **WHIP** (chiều ngược lại của WHEP).

- Luồng tồn tại khi tab trình duyệt còn mở. Chuyển giữa hai tab Phát/Xem không làm dừng luồng; đóng hoặc tải lại trang thì luồng mất (trình duyệt sẽ hỏi lại trước).
- Trình duyệt nén lại video, nên tốn CPU ở máy mở trang. Nếu mạng yếu, trình duyệt sẽ giảm fps (giữ nguyên độ phân giải).
- Video phải có định dạng trình duyệt phát được (MP4/H264, WebM…).

### Bằng ffmpeg (chạy lâu, không phụ thuộc tab)

```bash
ffmpeg -re -stream_loop -1 -i <file>.mp4 -c copy -an \
  -f rtsp -rtsp_transport tcp rtsp://localhost:8554/camera-upload
```

`-c copy` không nén lại nên gần như không tốn CPU. Tuy vậy WebRTC (FE) yêu cầu H264 **không có B-frame**. Kiểm tra video trước khi đẩy:

```bash
ffprobe -v error -show_entries stream=codec_name,has_b_frames <file>.mp4
```

Nếu ra `has_b_frames=1` hoặc codec không phải H264, thay `-c copy` bằng `-c:v libx264 -preset ultrafast -tune zerolatency -bf 0` (cách này tốn CPU).

## Port

Demo dùng các port sau, đều là mặc định của mediamtx:

| Port | Giao thức | Vai trò | Client |
|---|---|---|---|
| `8554/tcp` | RTSP | Nhận luồng đẩy vào, phát lại cho BE | ffmpeg, `reader.py`, VLC |
| `8889/tcp` | HTTP (WHIP/WHEP) | Bắt tay WebRTC: trao đổi SDP | `index.html` |
| `8189/udp` | WebRTC (ICE) | Truyền video giữa trình duyệt và mediamtx | `index.html` |
| `8890/udp` | SRT | Đọc luồng qua mạng kém ổn định (thử nghiệm) | ffplay, VLC, OBS |

Các giao thức không dùng (RTMP, HLS, MoQ, RTSP qua UDP) đã được tắt trong `mediamtx.yml`. Xem danh sách đầy đủ ở [Phụ lục](#phụ-lục-toàn-bộ-port-của-mediamtx).

> **Vì sao RTSP chỉ dùng TCP?** Mặc định RTSP có thể gửi video qua UDP (thêm port 8000/8001) và dễ bị rớt gói (`RTP packets lost`). Cấu hình `rtspTransports: [tcp]` gom mọi thứ về port 8554 và không mất gói.

### SRT

SRT là giao thức truyền video trên UDP, có cơ chế gửi lại gói bị mất. Vì vậy nó ổn định hơn RTSP khi đi qua internet hoặc 4G. Trình duyệt **không** hỗ trợ SRT, nên chỉ dùng được với ffmpeg, VLC, OBS hoặc BE.

```bash
ffplay "srt://localhost:8890?streamid=read:camera-upload"    # xem
ffprobe "srt://localhost:8890?streamid=read:camera-upload"   # kiểm tra thông tin luồng
```

`streamid` có dạng `read:<path>` khi đọc và `publish:<path>` khi đẩy luồng lên. Muốn tắt SRT thì thêm `srt: no` vào `mediamtx.yml`.

#### Vì sao SRT ổn định hơn

| Giao thức | Khi mất gói | Hậu quả |
|---|---|---|
| RTSP qua UDP | Bỏ qua gói mất | Hình vỡ, nhòe, giật |
| RTSP qua TCP | Chờ TCP gửi lại | Trên mạng kém, trễ dồn lại, video đứng hình |
| **SRT** | Tự gửi lại trong một khoảng chờ cố định (`latency`, mặc định 120ms) | Hình sạch, độ trễ ổn định |

SRT còn có sẵn mã hóa AES và đi qua NAT/firewall dễ hơn RTSP.

#### Khi nào dùng

1. **Camera ở xa gửi về server qua internet** (chi nhánh, nhà kho, công trường → cloud). RTSP được thiết kế cho LAN, nên thường đặt một máy nhỏ cạnh camera để chuyển RTSP sang SRT:
   ```
   camera ──RTSP (LAN)──► edge box ──SRT (internet)──► mediamtx trên cloud
   ```
2. **Thiết bị dùng 4G/5G** (camera trên xe, thiết bị di động), nơi băng thông thay đổi liên tục.
3. **Truyền hình và livestream chuyên nghiệp:** gửi tín hiệu từ hiện trường về studio. OBS, vMix và encoder phần cứng đều hỗ trợ.
4. **Chuyển luồng giữa các server**, ví dụ mediamtx ở site A đẩy sang site B.

#### Khi nào không cần

- Camera và server **cùng LAN**, như demo này: RTSP qua TCP là đủ.
- **Xem trên trình duyệt:** phải dùng WebRTC hoặc HLS.
- **Camera không hỗ trợ SRT** (phần lớn camera IP chỉ phát RTSP): cần thêm bước chuyển đổi như ở trường hợp 1.

## Cách FE nhận WebRTC

WebRTC không có URL kiểu `rtsp://` để mở trực tiếp. Kết nối gồm hai bước, chạy trên hai port khác nhau:

```
Trình duyệt                                   mediamtx
    │                                             │
    │ ① POST /<path>/whep  (SDP offer)  ──────►  │  :8889 HTTP
    │ ◄───────────────────────  (SDP answer)      │  (chỉ một lần)
    │                                             │
    │ ② ICE: thử các cặp địa chỉ, chọn cặp thông  │
    │ ◄═══════════ video H264 (liên tục) ═══════  │  :8189 UDP
```

1. **Signaling (8889):** trình duyệt gửi SDP offer, tức codec hỗ trợ và danh sách địa chỉ mạng của mình, rồi nhận SDP answer. Chuẩn này gọi là **WHEP**. Port này không chứa video.
2. **Media (8189):** `RTCPeerConnection` kết nối trực tiếp tới mediamtx và nhận video. `pc.ontrack` gắn luồng vào thẻ `<video>`.

Trong `index.html`:

```js
const pc = new RTCPeerConnection();                     // kết nối WebRTC (media, :8189)
pc.ontrack = e => video.srcObject = e.streams[0];       // video đổ vào <video>
const res = await fetch(WHEP, { body: offer.sdp });     // signaling (HTTP, :8889)
```

### ICE

ICE (Interactive Connectivity Establishment) là cơ chế tìm đường mạng giữa hai bên. Mỗi bên liệt kê các địa chỉ của mình, gọi là **candidates** (IPv4, IPv6, LAN…), rồi thử từng cặp và dùng cặp thông đầu tiên.

Đọc dòng log khi kết nối thành công:

```
peer connection established, local candidate: host/udp/2001:ee0:...:6151/8189,
                             remote candidate: prflx/udp/2001:ee0:...:6151/46249
```

| Thành phần | Ý nghĩa |
|---|---|
| `local candidate` | Địa chỉ phía mediamtx (port 8189) |
| `remote candidate` | Địa chỉ phía trình duyệt (port ngẫu nhiên) |
| `host` | Địa chỉ thật của card mạng |
| `prflx` | *Peer reflexive*: địa chỉ mediamtx phát hiện ra khi nhận gói kiểm tra. Bình thường |
| `udp` | Giao thức truyền video |

> **Vì sao dùng `network_mode: host`?** Nếu container chạy trong mạng riêng của Docker, mediamtx sẽ gửi cho trình duyệt IP nội bộ của container, và trình duyệt có thể không tới được. Dùng mạng của host thì candidate là IP thật của máy. Cách này chỉ chạy trên Linux. Trên macOS/Windows phải map port và khai báo `webrtcAdditionalHosts`.

## Xử lý sự cố

| Triệu chứng | Nguyên nhân / cách xử lý |
|---|---|
| FE báo `Failed to fetch` | Đang mở bằng `file://` hoặc `0.0.0.0`. Hãy mở qua `http://localhost:8080` |
| Mở `:8080` chỉ thấy danh sách file | Port 8080 đang bị một `python3 -m http.server` khác chiếm. Tắt nó rồi chạy `docker compose up -d` |
| Service `web` không chạy được | Port 8080 đang bị chiếm (ví dụ còn chạy `python3 serve.py` bằng tay). Kiểm tra bằng `ss -ltnp | grep 8080` |
| FE không hiện hình từ máy khác | Mở firewall cho `8080/tcp`, `8889/tcp`, `8189/udp` |
| `reader.py` báo `404 Not Found` | Chưa có ai đẩy vào path đó. Phát video ở tab Phát video và kiểm tra tên luồng trùng nhau |
| Log mediamtx im lặng | Bình thường, mediamtx chỉ log khi có kết nối mới hoặc lỗi. Log theo frame nằm ở terminal chạy `reader.py` |
| `RTP packets lost` | RTSP đang chạy qua UDP. Kiểm tra `rtspTransports: [tcp]` |

## Phụ lục: toàn bộ port của MediaMTX

Giá trị mặc định lấy từ cấu hình mẫu của image:

```bash
docker run --rm --entrypoint cat bluenviron/mediamtx:latest /mediamtx.yml
```

Đổi port bằng key tương ứng trong `mediamtx.yml`, ví dụ `rtspAddress: :9554`.

### Truyền và xem video

| Port | Giao thức | Key | Mục đích | Demo |
|---|---|---|---|---|
| 8554/tcp | RTSP | `rtspAddress` | Đẩy và đọc luồng RTSP | ✅ |
| 8000/udp | RTP | `rtpAddress` | Video khi RTSP chạy UDP | ❌ chỉ TCP |
| 8001/udp | RTCP | `rtcpAddress` | Thống kê khi RTSP chạy UDP | ❌ chỉ TCP |
| 8002–8003/udp | RTP/RTCP multicast | `multicastRTPPort`, `multicastRTCPPort` | Một luồng phát cho nhiều máy trong LAN | ❌ chỉ TCP |
| 8322/tcp | RTSPS | `rtspsAddress` | RTSP mã hoá TLS | ❌ `rtspEncryption: "no"` |
| 8004–8005/udp | SRTP/SRTCP | `srtpAddress`, `srtcpAddress` | RTP/RTCP mã hoá | ❌ |
| 8006–8007/udp | SRTP/SRTCP multicast | `multicastSRTPPort`, `multicastSRTCPPort` | Multicast mã hoá | ❌ |
| 8889/tcp | WebRTC HTTP | `webrtcAddress` | Signaling (WHEP/WHIP) | ✅ |
| 8189/udp | WebRTC ICE | `webrtcLocalUDPAddress` | Media WebRTC | ✅ |
| — | WebRTC ICE TCP | `webrtcLocalTCPAddress` | Dự phòng khi mạng chặn UDP | ❌ mặc định tắt |
| 1935/tcp | RTMP | `rtmpAddress` | OBS, livestream | ❌ `rtmp: no` |
| 1936/tcp | RTMPS | `rtmpsAddress` | RTMP mã hoá | ❌ `rtmp: no` |
| 8888/tcp | HLS | `hlsAddress` | Xem bằng `<video>`, trễ vài giây | ❌ `hls: no` |
| 8890/udp | SRT | `srtAddress` | Truyền qua mạng kém ổn định | ✅ |
| 8892/tcp+udp | MoQ HTTP2/3 | `moqHTTP2Address`, `moqHTTP3Address` | Thử nghiệm | ❌ `moq: no` |
| 8893/udp | MoQ QUIC | `moqQUICAddress` | Thử nghiệm | ❌ `moq: no` |

### Quản trị và giám sát (mặc định tắt)

| Port | Chức năng | Bật bằng | Mục đích |
|---|---|---|---|
| 9997/tcp | API | `api: yes` | REST API quản lý path và session |
| 9998/tcp | Metrics | `metrics: yes` | Số liệu dạng Prometheus |
| 9999/tcp | pprof | `pprof: yes` | Debug hiệu năng mediamtx |
| 9996/tcp | Playback | `playback: yes` | Tải lại video đã ghi |
