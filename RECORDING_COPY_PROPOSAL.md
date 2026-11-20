# Đề xuất: ghi video bằng cách copy luồng H.264 của camera

Phạm vi: `raspberry_pi_worker/realtime_hygiene_app` (feeder)
Trạng thái: đề xuất, chưa triển khai

## 1. Vấn đề

Feeder đang **decode** luồng camera, sau đó **encode lại** từ đầu để ghi MP4. Đo trên `attempt_1.mp4` (1920×1080, 25 fps, i5-11400H):

| Việc | CPU (% của 1 core) | Khi nào chạy |
|---|---|---|
| Decode 1080p | ~30% | Luôn luôn |
| **Ghi video 1080p** (libx264 71% + Python đẩy frame qua pipe 23%) | **~94%** | Khi đang rửa |
| Ghi video 640×360, để so sánh | ~14% | Khi đang rửa |

Máy triển khai đang ghi ở 1080p (`RECORD_WIDTH/HEIGHT` trong `.env`). Trên máy N150 tỉ lệ còn cao hơn.

Code không copy luồng camera vì recorder được port nguyên từ edge-recorder, vốn thiết kế để nhận frame BGR. Không có tài liệu nào cho thấy phương án copy đã được cân nhắc.

## 2. Giải pháp

Mở **một** kết nối RTSP bằng **PyAV** (thư viện của `FFmpeg`, chạy trong process) và tách thành hai đường:

```
RTSP (H.264) ──► PyAV demux
                   ├─ gói H.264 gốc ──► copy thẳng vào MP4      (không decode, không encode)
                   └─ decode ──► gate / scorer AI               (giữ như hiện tại)
```

- Ghi video: **~94% → gần 0%** CPU. Chất lượng video giữ nguyên như camera, không bị nén thêm lần nào.
- Decode cho AI không đổi. Prototype dùng PyAV đo được ~37%, so với ~30% khi dùng OpenCV.

### Vì sao chọn PyAV

| Cách | Đánh giá |
|---|---|
| ffmpeg CLI xuất 2 output | Không bật/tắt việc ghi theo từng lần rửa được. Frame trả qua pipe mất timestamp, nên không căn được video với kết quả AI. Muốn làm thì phải ghi segment vòng và tự quản lý offset. |
| GStreamer (tee + appsink + splitmuxsink) | Làm được, nhưng phải thêm PyGObject và các plugin vào image. Pipeline động (pre-roll, chờ keyframe) phức tạp, không có lợi gì hơn PyAV. |
| **PyAV** | Vẫn là FFmpeg, nhưng điều khiển được từng gói tin và có timestamp chính xác cho từng frame. Đã chạy prototype trên `attempt_1.mp4`: file copy ra là H.264 High 1080p, bắt đầu bằng I-frame. |

## 3. Thiết kế chính

1. **Bắt đầu từ keyframe.** Video copy chỉ có thể bắt đầu ở keyframe. Feeder giữ sẵn các gói tin của 2 GOP gần nhất (khoảng 2–3 MB). Đồng thời scorer giữ ảnh 224×224 từ keyframe đó (khoảng 7.5 MB), để khi bắt đầu rửa thì chấm luôn cả phần pre-roll này.
   → Giây 0 của video trùng với frame 0 của AI, nên **evidence, history, EHP và rescore không phải sửa**.
2. **Lấy mẫu 25 fps theo timestamp** (`FpsGrid`), dùng chung cho scorer và rescore. Camera nhanh hơn 25 fps thì bỏ bớt frame, chậm hơn thì lặp frame. Cách này sửa luôn lỗi hiện tại: khi camera dưới 25 fps, thời lượng rửa bị tính ngắn đi.
3. **Mất kết nối giữa lúc rửa.** Kết nối mới bắt đầu ghi từ keyframe đầu tiên, timestamp được nối tiếp ngay sau gói cuối của kết nối cũ. Scorer cũng bỏ qua khoảng mất tín hiệu theo cùng cách, nên video và AI vẫn khớp nhau.
4. **Đường dự phòng (giữ code hiện tại).** Feeder quay về cách encode như bây giờ khi:
   - camera xuất HEVC (trình duyệt không phát được),
   - đặt `RECORD_COPY=0` để rollback mà không cần deploy lại,
   - GOP của camera dài quá 10 s.

## 4. File cần sửa

| File | Thay đổi |
|---|---|
| `camera/feeder.py` | Thêm `StreamReader` (PyAV) thay cho `cv2.VideoCapture`, thêm `CopyRecorder`. Thêm pre-roll vào `camera_loop`. `ScorerWorker` và `SessionRecorder` nhận frame theo lưới 25 fps. |
| `ai_hygiene/triton_stream.py` | Thêm `FpsGrid`. |
| `ai_hygiene/rescore.py` | Lấy mẫu frame theo `FpsGrid`, vì video copy chạy theo fps gốc của camera. |
| `config.py`, `.env.example` | Thêm `RECORD_COPY` (mặc định 1). `RECORD_WIDTH/HEIGHT` chỉ còn áp dụng cho đường dự phòng. |
| `requirements.txt` | Thêm `av` (image nặng thêm khoảng 60 MB). |
| `tests/` | Test cho `FpsGrid`, `CopyRecorder` (pre-roll, reconnect) và rescore với video không phải 25 fps. |

## 5. Cấu hình camera bắt buộc (luồng 101)

- Codec: **H.264**
- I-frame interval: **50** (2 s ở 25 fps)
- **Tắt H.264+ / Smart Codec.** Chế độ này làm GOP rất dài.
- Frame rate: 25 fps

## 6. Rủi ro và kiểm chứng

| Rủi ro | Cách kiểm tra |
|---|---|
| Chưa test với RTSP thật và trên N150 | Chạy feeder trên N150 với camera thật, đo CPU bằng `pidstat -t` |
| Verdict thay đổi do có thêm pre-roll và lấy mẫu theo timestamp | Cho vài người rửa tay, chạy cả cách cũ và cách mới, so verdict, số lần lặp và thời gian từng bước |
| Trình duyệt không phát được profile H.264 của camera | Mở video ở `/history` trên Chromium của kiosk và trên web EHP |
| Dung lượng file bằng bitrate camera (~4–5 Mbps) | Tương đương bản 1080p hiện tại (~5.4 Mbps), không tăng thêm |

## 7. Ngoài phạm vi

- **Render evidence** (`evidence.py`) vẫn encode lại video 1080p để chèn overlay. Bước này chạy nền (`nice`, 2 thread) sau khi session đóng. Nếu cần giảm tiếp, có thể dùng encode phần cứng (`h264_vaapi`).
- **Decode bằng phần cứng** (VAAPI) để giảm tiếp phần ~30% decode. Bản PyAV và OpenCV cài từ pip không có sẵn VAAPI, nên sẽ là một bước riêng.
