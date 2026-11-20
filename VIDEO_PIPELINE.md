# Video Pipeline: từ luồng RTSP đến AI và video bằng chứng

Tài liệu chi tiết về các hàm chính của realtime hygiene app: lấy luồng RTSP, decode, đưa qua AI, ghi lại video, render evidence và đồng bộ lên hệ thống. Mỗi bước ghi rõ chạy trên **CPU hay iGPU**.

> Cấu hình giả định: máy triển khai đặt kích thước ghi video **1920×1080** trong `.env` (`RECORD_WIDTH/HEIGHT` hoặc `EDGE_FRAME_WIDTH/HEIGHT`). Đã kiểm tra trên file `attempt_1.mp4`: 1920×1080, 25 fps, ~5.4 Mbps, libx264 superfast crf 26.
> Đề xuất tối ưu phần ghi video nằm ở `RECORDING_COPY_PROPOSAL.md`.

---

## 1. Tổng quan

Một container (`realtime-hygiene`) chạy hai process, do `realtime_entrypoint.py` giám sát:

| Process | Lệnh | Việc |
|---|---|---|
| **feeder** | `python -m camera.feeder` | Lấy RTSP, decode, hand gate, chấm điểm qua Triton, ghi MP4 |
| **server** | `uvicorn app:app` | Kiosk state machine, WHO validator, history, render evidence, rescore, MinIO/Kafka |

Hai process trao đổi qua HTTP `POST 127.0.0.1:8090/events`.

```
 Camera IP (RTSP/TCP, H.264 1080p)
     │
     ▼
 ┌──────────────────────────────── feeder ────────────────────────────────┐
 │ [1] cv2.VideoCapture (FFmpeg) ── decode CPU ──► frame BGR 1920×1080      │
 │        │                                                                │
 │        ├──► [2] Hand gate  hand_320 / OpenVINO ── iGPU (fallback CPU)    │
 │        │        thấy tay → wash_started ; mất tay 3 s → wash_stopped     │
 │        │                                                                │
 │        ├──► [3] ScorerWorker  resize 224 ── gRPC ──► Triton (máy khác)   │
 │        │        AI stage + AI count → step_frame_batch / step_counts     │
 │        │                                                                │
 │        └──► [4] SessionRecorder → ffmpeg libx264 ── encode CPU → MP4     │
 └───────────────────────────────┬────────────────────────────────────────┘
                                 │ POST /events
 ┌───────────────────────────────▼──────── server ───────────────────────┐
 │ [5] WHO validator → verdict → kiosk (WebSocket)                         │
 │ [6] Session đóng → thumbnail → render evidence (libx264, CPU)           │
 │     → rescore (nếu cần) → upload MinIO → Kafka HandwashEvent             │
 └────────────────────────────────────────────────────────────────────────┘
```

---

## 2. Phần cứng sử dụng: CPU hay iGPU?

| Bước | Thư viện | Phần cứng | Ghi chú |
|---|---|---|---|
| Decode RTSP | FFmpeg trong `opencv-python-headless` | **CPU** | Bản OpenCV cài từ pip được build `VA: NO`, nên không dùng được VAAPI/QSV |
| Hand gate (AI #1) | OpenVINO `hand_320` | **iGPU** nếu `HAND_DEVICE=GPU` và container có `/dev/dri`, ngược lại CPU | Base image `openvino/ubuntu24_runtime` có sẵn Intel GPU compute runtime. iGPU chỉ được map khi chạy kèm `docker-compose.minipc.yml` |
| Resize, chuyển màu | OpenCV | CPU (SIMD) | Rất nhẹ |
| Chấm bước, đếm lần lặp (AI #2, #3) | Triton qua gRPC | **Máy chạy Triton** (edge Triton hoặc cloud) | Edge box chỉ resize ảnh và gửi đi |
| Ghi MP4 | `ffmpeg` libx264 (tiến trình con) | **CPU** | Không dùng `h264_vaapi`/`h264_qsv` |
| Render evidence | `ffmpeg` libx264 + libass | **CPU** | `nice -n 15`, `-threads 2`, mỗi lần một attempt |
| Thumbnail, rescore | OpenCV | CPU | Rescore còn gọi Triton |

Cách kiểm tra hand gate có thật sự chạy trên iGPU:

```bash
docker logs realtime-hygiene | grep "OpenVINO hand gate ready"   # phải thấy device=GPU
```

Nếu log hiện `OpenVINO device GPU unavailable; using CPU` thì gate đang chạy CPU. Theo chú thích trong code, mỗi lần chạy trên CPU tốn khoảng 150 ms.

**Tóm lại:** hiện chỉ có hand gate dùng iGPU. Decode, encode và render đều chạy CPU.

---

## 3. Chi tiết từng bước

### [1] Lấy luồng RTSP và decode

| | |
|---|---|
| Hàm | `camera_loop()` (`camera/feeder.py:761`) |
| Mở luồng | `cv2.VideoCapture(url, cv2.CAP_FFMPEG)` (`feeder.py:806`) |
| Tùy chọn | `set_capture_options()` (`feeder.py:75`): `rtsp_transport=tcp`, `fflags=nobuffer`, `flags=low_delay` |
| Kết quả | frame BGR `uint8` kích thước gốc của camera (luồng 101: 1920×1080), **mỗi frame camera gửi về** |

- **Vì sao dùng TCP:** UDP dễ mất gói, ảnh HEVC bị nhòe hoặc xám.
- **Mất luồng:** feeder thử kết nối lại mỗi 5 s (`RECONNECT_DELAY_S`). Nếu đang rửa, attempt vẫn được giữ trong `MOTION_TIMEOUT_S + STREAM_GRACE_S` (3 + 5 s) và được đánh dấu `stream_interrupted`.
- **Thống kê:** cứ mỗi `CAPTURE_STATS_S`, feeder log capture fps, số lần gate chạy mỗi giây và độ trễ so với luồng.
- **Trạng thái camera:** `CameraStatusReporter` (`feeder.py:693`) báo ONLINE/OFFLINE/RECONNECTING cho server, server gửi tiếp lên EHP qua Kafka mỗi 60 s.

### [2] Hand gate: AI #1, quyết định khi nào bắt đầu một lần rửa

| | |
|---|---|
| Hàm | `build_gate()` (`feeder.py:668`) → `HandGate` (`camera/hand_gate.py:48`), chạy trong `LatestFrameGate` (`feeder.py:551`) |
| Model | `models/hand_320.xml/.bin` (OpenVINO IR, một lớp: bàn tay) |
| Input | `float32 [1, 3, 320, 320]`, RGB, giá trị 0..1 |
| Output | `[1, 300, 6]` = `x1, y1, x2, y2, confidence, class` (đã qua NMS) |
| Tần suất | cứ 2 frame xét một lần (`detect_every_n_frames=2`) và tối đa 4 lần/giây (`GATE_MAX_HZ`) |

Tiền xử lý trong `_preprocess()` (`hand_gate.py:141`):

```
1920×1080 ──thu nhỏ giữ tỉ lệ (×0.1667)──► 320×180 ──thêm viền xám 114 (70 px trên/dưới)──► 320×320 ──► RGB /255 ──► NCHW
```

Hậu xử lý trong `infer()` (`hand_gate.py:109`):

- Giữ box có `confidence ≥ 0.70` (`HAND_CONFIDENCE`).
- Đổi tọa độ box ngược về khung hình gốc.
- Chỉ giữ box có tâm nằm trong `HAND_ZONE` (vùng bồn rửa, tọa độ chuẩn hóa).

Gate chạy trên **thread riêng** và luôn lấy **frame mới nhất**, nên không làm chậm vòng capture. Nếu không có OpenVINO, feeder dùng `MotionGate` (MOG2 trên ảnh 320×180, không phải AI).

**Máy trạng thái trong `camera_loop`:**

- Thấy tay khi đang idle → `wash_started`. Tạo `ScorerWorker` và `SessionRecorder`. Nếu `TRIGGER_DEBUG=1`, lưu ảnh trigger vào `sessions/trigger_debug/`.
- Không thấy tay trong `MOTION_TIMEOUT_S` (3 s) → `end_wash()`. Hàm này tách attempt khỏi vòng capture, sau đó `SerialWorker` (`feeder.py:604`) chạy nền: flush scorer, đóng MP4, gửi `wash_stopped`.
- Các event quan trọng (`wash_started`, `wash_stopped`, `feeder_started`) được gửi lại tối đa 5 lần (`post_event`, `feeder.py:104`).

### [3] Chấm điểm: AI #2 (stage) và AI #3 (count) trên Triton

| | |
|---|---|
| Hàm | `ScorerWorker` (`feeder.py:239`) → `HygieneScorer` (`ai_hygiene/triton_stream.py:323`) → `TritonStepClassifier` (`triton_stream.py:170`) |
| Chọn Triton | `TritonRouter.candidates()` (`ehp_integration/triton_router.py:289`): EHP chỉ định dùng edge hay cloud, có fallback sang bên còn lại |

**Bước 3.1: chuẩn bị frame.** `ScorerWorker.submit()` (`feeder.py:318`) làm ba việc:

1. Lấy mẫu **25 fps** theo đồng hồ của feeder.
2. `cv2.resize(1920×1080 → 224×224)`. Ảnh bị **ép thẳng** thành hình vuông (không giữ tỉ lệ), giống cách cloud worker và cách lúc train.
3. Đổi BGR → RGB, đưa vào hàng đợi `uint8 (224, 224, 3)` có sức chứa 60 s (`SCORER_BUFFER_S`).

**Bước 3.2: gọi model.** Thread scorer gọi `HygieneScorer.feed()` (`triton_stream.py:394`):

| Model | Input | Output | Tần suất |
|---|---|---|---|
| **Stage** (`hygiene_handwashing_monitoring_inference_pipeline`) | `FRAMES uint8 (24, 224, 224, 3)` | `PROBS float32 (24, 7)`: xác suất lớp 0..6 cho từng frame | cửa sổ 24 frame, trượt 12 frame (≈ 0.48 s), chồng nhau 50% |
| **Count** (`hygiene_handwashing_monitoring_count_by_steps_pipeline`) | `FRAMES (24, 224, 224, 3)` + `GLOBAL_STEPS int64 (24)` | `COUNTS float32 (6)`: số lần lặp của từng bước **trong block này**; `HygieneScorer` cộng dồn qua các block (`triton_stream.py:388`) | block 24 frame không chồng nhau (≈ 0.96 s) |

Xử lý sau khi nhận kết quả stage:

1. Lấy trung bình xác suất ở các phần cửa sổ chồng nhau.
2. Làm mượt bằng `CausalProbSmoother` (cửa sổ 15 frame).
3. Lấy argmax ra **một nhãn bước cho mỗi frame**.

Kết quả gửi sang server qua `step_frame_batch` và `step_counts`.

**Bước 3.3: đường truyền** (`infer_probs`/`infer_counts`, `triton_stream.py:288/307`):

| Pipeline | Dữ liệu gửi đi | Dùng khi |
|---|---|---|
| `standard` | frame thô, mỗi clip ≈ 3.6 MB | Triton trên edge (mặc định, qua LAN) |
| `features` | stage gửi frame; count dùng lại feature `(1280, 24)` ≈ 123 KB | Triton trên cloud (mặc định) |
| `jpeg` | 24 ảnh JPEG 224×224 | Băng thông thấp, chất lượng giảm |

Khi gọi cloud, frame còn được giảm xuống 6 bit màu (`CLOUD_TRITON_BITS`) và nén gzip.

**Bước 3.4: khi mạng xấu.** Triton gọi lỗi thì thử lại với backoff tăng dần đến 5 s.

- Scorer trễ hơn `SCORER_LAG_WARN_S` → kiosk hiện popup "network".
- Hàng đợi đầy, hoặc sau khi rửa xong mà không chấm hết trong `SCORER_DRAIN_TIMEOUT_S` (60 s) → attempt được đánh dấu `network_interrupted` và **chấm lại từ video** sau (bước [6]).

### [4] Ghi video thô

| | |
|---|---|
| Hàm | `SessionRecorder` (`feeder.py:164`) → `FfmpegH264Writer` (`feeder.py:130`) |
| Kích thước | `RECORD_WIDTH × RECORD_HEIGHT`, hiện là **1920×1080** theo `.env` (mặc định trong code là 640×360) |
| Encoder | `ffmpeg -c:v libx264 -preset superfast -tune zerolatency -crf 26 -g 50 -pix_fmt yuv420p` |
| Container | MP4 dạng fragmented (`frag_keyframe+empty_moov+default_base_moof`): nếu tiến trình bị kill, file vẫn phát được đến 2 s cuối |
| Lưu ở | `sessions/incoming/<cam>_<ms>.mp4` |

Đường đi của một frame:

```
frame BGR 1920×1080 ─► lấy mẫu 25 fps (cùng lịch với scorer) ─► hàng đợi 64 frame
   ─► thread nền: cv2.resize(1920×1080) ─► .tobytes() ─► pipe ─► ffmpeg libx264 ─► MP4
```

- Lấy mẫu cùng lịch với scorer, nên **frame thứ i của MP4 ứng với nhãn bước thứ i**. Evidence, history và rescore đều dựa vào điều này.
- Nếu encode không theo kịp, frame cũ nhất trong hàng đợi bị bỏ và có log cảnh báo.
- **Video không copy thẳng từ luồng camera.** Feeder decode rồi encode lại từ đầu. Đây là thiết kế kế thừa từ recorder của edge-recorder, vốn nhận frame BGR.

### [5] Server: WHO validator và verdict

| | |
|---|---|
| Hàm | `post_event()` (`app.py:286`), `WhoHandwashValidator` (`ai_hygiene/who_validator.py`) |

1. `wash_started` → tạo attempt mới và reset validator. Kiosk chuyển sang "washing".
2. `step_frame_batch` → gọi `validator.update(step_id)` cho từng frame. Validator coi mỗi frame dài **1/25 s**.
3. `step_counts` → `validator.set_raw_counts()`. Kiosk hiện tiến độ x/5.
4. `wash_stopped` → `validator.result()` (`who_validator.py:217`). Luật kiểm tra: đủ 6 bước, mỗi bước ≥ 5 lần, tổng thời gian ≥ 30 s, các đoạn ngắn hơn 0.5 s bị bỏ.
5. Lần rửa không có bước WHO nào (người đi ngang qua) thì bị bỏ, xóa video. Còn lại hiện ✅ hoặc ⚠️ trong 12 s.

### [6] Sau khi session đóng: các worker nền trong `SessionManager` (`storage/session_store.py`)

| Worker | Hàm | Việc | Phần cứng |
|---|---|---|---|
| Render evidence | `_render_loop` → `render_attempt()` (`ai_hygiene/evidence.py:389`) | Tạo overlay ASS (bước hiện tại, checklist, tổng kết), rồi `ffmpeg libx264 veryfast crf26 -threads 2`, `nice 15` → `attempt_N_evidence.mp4` | CPU |
| Thumbnail | `_ensure_thumbnail` (`session_store.py:529`) | Lấy frame đầu của MP4 thô làm JPEG | CPU |
| Rescore | `_rescore_loop` → `rescore_video()` (`ai_hygiene/rescore.py:18`) | Đọc MP4 thô, resize 224, chấm lại qua Triton như bước [3] | CPU + Triton |
| Sync | `_sync_once` / `_sync_session` (`session_store.py:438/457`) | Upload video thô, evidence và thumbnail lên MinIO; gửi `HandwashEvent` lên Kafka | Mạng |

---

## 4. Các kích thước ảnh trong pipeline (với `.env` 1920×1080)

| Đích | Từ | Thành | Giữ tỉ lệ | Tần suất |
|---|---|---|---|---|
| Hand gate (AI #1) | 1920×1080 | 320×180 + viền → **320×320** | ✅ letterbox | ≤ 4 lần/s, luôn chạy |
| Stage / count (AI #2, #3) | 1920×1080 | **224×224** | ❌ ép thẳng | 25 fps, khi đang rửa |
| MP4 thô | 1920×1080 | **1920×1080** (resize không làm gì nhưng vẫn copy dữ liệu) | ✅ | 25 fps, khi đang rửa |
| Evidence | MP4 thô | 1920×1080 (overlay thiết kế trên canvas 1920×1080) | ✅ | 1 lần/attempt, chạy nền |
| Rescore | MP4 thô | 224×224 | ❌ | khi mạng lỗi |

Model chỉ dùng tới 320×180 và 224×224. Kích thước 1080p **chỉ có ích cho video bằng chứng** (người xem), không giúp gì cho độ chính xác của AI.

---

## 5. Chi phí CPU đo được

Đo trên i5-11400H với `attempt_1.mp4`. Máy N150 yếu hơn, tỉ lệ thực tế sẽ cao hơn.

| Việc | CPU | Khi nào |
|---|---|---|
| Decode 1080p (`cv2.VideoCapture`) | ~30% của 1 core | **Luôn luôn**, kể cả lúc không có ai |
| Resize 224 + đổi màu cho scorer | < 1% | Khi đang rửa |
| **Ghi MP4 1080p** (libx264 ~71% + Python resize/pipe ~23%) | **~94%** | Khi đang rửa |
| Ghi MP4 640×360 (để so sánh) | ~14% | Khi đang rửa |
| Render evidence 1080p | ~13.5 s CPU cho mỗi 22 s video (4.2 s thời gian thực) | Chạy nền sau khi session đóng |
| Hand gate | chạy trên iGPU (chưa đo); trên CPU khoảng 150 ms/lần × 4/s | Luôn luôn |

**Khi đang có người rửa tay, phần nặng nhất là ghi video (encode lại 1080p), không phải AI.**

---

## 6. Biến cấu hình liên quan

| Biến | Mặc định | Ý nghĩa |
|---|---|---|
| `EDGE_CAMERAS` | – | JSON gồm đúng 1 camera (`name`, `rtsp_url`, `hand_zone`) |
| `RTSP_TRANSPORT` | `tcp` | Giao thức truyền RTSP |
| `MOTION_BACKEND` | `openvino` | `openvino` (hand_320) hoặc `mog2` |
| `HAND_DEVICE` | `GPU` | Thiết bị OpenVINO; tự chuyển về CPU nếu không có GPU |
| `HAND_ZONE` | – | Vùng bồn rửa `x1,y1,x2,y2` (chuẩn hóa) |
| `HAND_CONFIDENCE` | 0.70 | Ngưỡng độ tin cậy của gate |
| `GATE_MAX_HZ` | 4 | Số lần chạy gate tối đa mỗi giây |
| `MOTION_TIMEOUT_S` | 3 | Không thấy tay bao lâu thì kết thúc lần rửa |
| `STREAM_GRACE_S` | 5 | Thời gian giữ attempt khi mất luồng |
| `RECORD_SESSIONS` | 1 | Có ghi MP4 hay không |
| `RECORD_WIDTH/HEIGHT` (alias `EDGE_FRAME_WIDTH/HEIGHT`) | 640×360 (**đang đặt 1920×1080**) | Kích thước video thô |
| `RECORD_PRESET` / `RECORD_CRF` | `superfast` / 26 | Tham số libx264 |
| `TRITON_URL` / `CLOUD_TRITON_URL` | – | Triton trên edge / cloud |
| `EDGE_TRITON_PIPELINE` / `CLOUD_TRITON_PIPELINE` | `standard` / `features` | Cách gửi dữ liệu lên Triton |
| `CLOUD_TRITON_BITS` / `CLOUD_TRITON_COMPRESSION` | 6 / `gzip` | Giảm dung lượng khi gửi lên cloud |
| `SCORER_BUFFER_S` / `SCORER_DRAIN_TIMEOUT_S` | 60 / 60 | Bộ đệm và thời gian chờ chấm hết khi mạng chậm |

---

## 7. Các hướng tối ưu (chưa triển khai)

| Hướng | Lợi ích | Chi tiết |
|---|---|---|
| Copy luồng H.264 thay vì encode lại | Ghi video ~94% → gần 0% | `RECORDING_COPY_PROPOSAL.md` |
| Encode bằng phần cứng (`h264_vaapi`/`h264_qsv`) cho MP4 thô và evidence | Giảm phần lớn chi phí encode | Cần driver Intel media trong image |
| Decode bằng phần cứng (VAAPI) | Decode ~30% → vài % | Bản OpenCV/PyAV cài từ pip không có VAAPI; cần decode bằng `ffmpeg` hoặc tự build |
| Ghi ở 1280×720 thay vì 1080p | Chỉ cần sửa `.env` | Video bằng chứng kém nét hơn một chút |
